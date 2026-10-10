// Injected into wx2.qq.com on every page load. Waits until the web WeChat Angular
// app has logged in, then forwards every message to Python (window.__lcPush) and
// exposes window.__lcSend(peer, text) for replies.
(() => {
  if (window.top !== window) return; // init scripts also run in iframes
  const TYPE_LABEL = {
    3: '[图片]', 34: '[语音]', 42: '[名片]', 43: '[视频]', 47: '[表情]',
    48: '[位置]', 49: '[链接/文件]', 62: '[视频]', 10002: '[撤回了一条消息]',
  };
  const SKIP_TYPES = new Set([51, 9999, 10000]); // status notify, sys notice, system tip
  const BOT_ECHO_WINDOW_MS = 120000;

  const botSent = []; // {peer, text, at}: lets us tell our own sends apart from the human's
  const forwarded = new Set();

  function toText(html) {
    return String(html || '')
      .replace(/<(?:img|span)[^>]*class="emoji emoji([0-9a-f]+)"[^>]*>(?:<\/span>)?/g,
        (_, hex) => (hex.length <= 5 ? String.fromCodePoint(parseInt(hex, 16)) : ''))
      .replace(/<img[^>]*text="([^"]*?)(?:_web)?"[^>]*>/g, '$1')
      .replace(/<br\s*\/?>/g, '\n')
      .replace(/<[^>]+>/g, '')
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
      .trim();
  }

  function isBotEcho(peer, text) {
    const now = Date.now();
    while (botSent.length && now - botSent[0].at > BOT_ECHO_WINDOW_MS) botSent.shift();
    return botSent.some(b => b.peer === peer && b.text === text);
  }

  function install() {
    if (window.__lcInstalled) return true;
    if (!window.angular) return false;
    const inj = angular.element(document.body).injector();
    if (!inj) return false;
    const cf = inj.get('chatFactory'), ctf = inj.get('contactFactory');
    const acc = inj.get('accountFactory'), conf = inj.get('confFactory');
    if (!acc.getUserName()) return false; // QR code not scanned yet

    function forward(m) {
      if (SKIP_TYPES.has(m.MsgType) || forwarded.has(String(m.MsgId))) return;
      const me = acc.getUserName();
      const fromMe = m.FromUserName === me;
      const peer = fromMe ? m.ToUserName : m.FromUserName;
      const isGroup = peer.startsWith('@@');
      const c = ctf.getContact(peer) || {};
      const text = m.MsgType === 1
        ? toText(m.MMActualContent || m.Content)
        : (TYPE_LABEL[m.MsgType] || `[消息类型${m.MsgType}]`) + (m.MsgType === 49 && m.FileName ? ' ' + m.FileName : '');
      if (fromMe && isBotEcho(peer, text)) return;
      forwarded.add(String(m.MsgId));

      let sender = '我';
      if (!fromMe) {
        if (isGroup) {
          const s = m.MMActualSender && ctf.getContact(m.MMActualSender, peer);
          sender = s ? (s.RemarkName || s.DisplayName || s.NickName) : '群友';
        } else {
          sender = c.RemarkName || c.NickName || '对方';
        }
      }
      let mentionsMe = false;
      if (isGroup && !fromMe) {
        const self = (c.MemberList || []).find(x => x.UserName === me) || {};
        const names = [acc.getUserInfo().NickName, self.DisplayName].map(toText).filter(Boolean);
        // An @ of my name, or a quote-reply to one of my messages ("<my name>: ...").
        mentionsMe = names.some(n => text.includes('@' + n) || text.startsWith(`"${n}:`));
      }
      window.__lcPush({
        id: String(m.MsgId), peer, isGroup, fromMe, type: m.MsgType, text, time: m.CreateTime,
        peerNick: toText(c.NickName), peerRemark: c.RemarkName || '', sender: toText(sender), mentionsMe,
      });
    }

    const orig = cf.messageProcess;
    cf.messageProcess = function (m) {
      const ret = orig.apply(this, arguments);
      try { forward(m); } catch (e) { console.warn('[lc] forward failed', e); }
      return ret;
    };

    window.__lcSend = async (peer, text) => {
      botSent.push({peer, text, at: Date.now()});
      const msg = cf.createMessage({ToUserName: peer, MsgType: conf.MSGTYPE_TEXT, Content: text});
      cf.appendMessage(msg);
      cf.sendMessage(msg);
      for (let i = 0; i < 40 && msg.MMStatus === conf.MSG_SEND_STATUS_SENDING; i++) {
        await new Promise(r => setTimeout(r, 300));
      }
      forwarded.add(String(msg.MsgId));
      return {ok: msg.MMStatus === conf.MSG_SEND_STATUS_SUCC, id: String(msg.MsgId)};
    };

    // Stickers: Python uploads the file (no CORS, real error messages) using what this
    // returns, then __lcSendEmoticon posts the uploaded MediaId as a sticker (MsgType 47).
    const util = inj.get('utilFactory');
    const confUrl = (re, fallback) => {
      for (const k in conf) if (typeof conf[k] === 'string' && re.test(conf[k])) return conf[k].split('?')[0];
      return fallback;
    };
    window.__lcUploadInfo = () => ({
      uploadUrl: confUrl(/webwxuploadmedia/, `https://file.${location.host}/cgi-bin/mmwebwx-bin/webwxuploadmedia`),
      baseRequest: acc.getBaseRequest(), passTicket: acc.getPassticket() || '',
      dataTicket: util.getCookie('webwx_data_ticket') || '', me: acc.getUserName(),
    });
    window.__lcSendEmoticon = async (peer, mediaId) => {
      const sendUrl = confUrl(/webwxsendemoticon/, '/cgi-bin/mmwebwx-bin/webwxsendemoticon');
      botSent.push({peer, text: '[表情]', at: Date.now()});
      const localId = String(Date.now()) + String(Math.random()).slice(2, 6);
      const body = {
        BaseRequest: acc.getBaseRequest(), Scene: 0,
        Msg: {Type: 47, EmojiFlag: 2, MediaId: mediaId, FromUserName: acc.getUserName(), ToUserName: peer,
              LocalID: localId, ClientMsgId: localId},
      };
      let sent;
      try {
        sent = await fetch(`${sendUrl}?fun=sys&pass_ticket=${encodeURIComponent(acc.getPassticket() || '')}`, {
          method: 'POST', body: JSON.stringify(body), credentials: 'include',
          headers: {'Content-Type': 'application/json;charset=UTF-8'},
        }).then(r => r.json());
      } catch (e) { return {ok: false, step: 'send', detail: `${sendUrl}: ${e}`}; }
      const ok = !!(sent && sent.BaseResponse && sent.BaseResponse.Ret === 0);
      if (ok && sent.MsgID) forwarded.add(String(sent.MsgID));
      return {ok, id: String((sent && sent.MsgID) || ''), step: 'send', detail: ok ? null : sent};
    };

    // Look up a chat's UserName by 备注名 (friend) or group-name substring, for chats
    // that haven't had any traffic yet in this login.
    window.__lcFind = (match) => {
      const hit = Object.values(ctf.getAllContacts()).find(c => {
        const isGroup = c.UserName.startsWith('@@');
        if (match.remark) return !isGroup && c.RemarkName === match.remark;
        return isGroup && toText(c.NickName).includes(match.group);
      });
      return hit ? hit.UserName : '';
    };

    window.__lcInstalled = true;
    console.log('[lc] hook installed');
    return true;
  }

  const timer = setInterval(() => {
    try { if (install()) clearInterval(timer); } catch (e) { /* app still booting */ }
  }, 1000);
})();
