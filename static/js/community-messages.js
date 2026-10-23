/* Private Community Messages. User text is always assigned with textContent. */
(function () {
  'use strict';
  var BOOT = window.GM_MESSAGES_BOOT || {};
  var state = { conversations: [], current: null, currentUser: null, timer: null };

  function $(id) { return document.getElementById(id); }
  function api(path, options) {
    var opts = options || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return fetch(path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(data.error || 'Request failed.');
          error.data = data;
          throw error;
        }
        return data;
      });
    });
  }
  function escapeName(name) { return String(name || 'Community member').replace(/^@+/, ''); }
  function timeAgo(value) {
    var then = new Date(value || '').getTime(); if (!then) return '';
    var mins = Math.max(0, Math.floor((Date.now() - then) / 60000));
    if (mins < 1) return 'now'; if (mins < 60) return mins + 'm';
    var hours = Math.floor(mins / 60); if (hours < 24) return hours + 'h';
    return Math.floor(hours / 24) + 'd';
  }
  function avatar(host, item) {
    if (!host) return;
    host.textContent = escapeName(item.username).charAt(0).toUpperCase();
    if (item.avatar_url) {
      var img = document.createElement('img');
      img.src = item.avatar_url; img.alt = ''; img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.onerror = function () { img.remove(); };
      host.textContent = ''; host.appendChild(img);
    }
  }
  function toast(message) {
    var node = $('gmMsgToast'); if (!node) return;
    node.textContent = message; node.hidden = false;
    clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.hidden = true; }, 2800);
  }
  function showInbox() {
    var inbox = $('gmMsgInbox'), thread = $('gmMsgThread');
    if (inbox) inbox.hidden = false;
    if (thread) thread.hidden = true;
    state.current = null; stopPolling(); loadInbox();
  }
  function showThread() {
    var inbox = $('gmMsgInbox'), thread = $('gmMsgThread');
    if (inbox) inbox.hidden = true;
    if (thread) thread.hidden = false;
  }
  function renderInbox() {
    var list = $('gmMsgList'), empty = $('gmMsgEmpty'), total = $('gmMsgTotal');
    if (!list || !empty) return;
    list.textContent = ''; empty.hidden = state.conversations.length !== 0;
    state.conversations.forEach(function (item) {
      var row = document.createElement('button'); row.type = 'button'; row.className = 'gm-msg-row';
      var image = document.createElement('span'); image.className = 'gm-msg-avatar'; avatar(image, item); row.appendChild(image);
      var main = document.createElement('span'); main.className = 'gm-msg-row-main';
      var name = document.createElement('span'); name.className = 'gm-msg-row-name';
      name.textContent = '@' + escapeName(item.username); main.appendChild(name);
      var preview = document.createElement('span'); preview.className = 'gm-msg-row-preview';
      preview.textContent = item.last_message || 'Start a conversation'; main.appendChild(preview); row.appendChild(main);
      var meta = document.createElement('span'); meta.className = 'gm-msg-row-meta';
      var time = document.createElement('span'); time.textContent = timeAgo(item.last_message_at); meta.appendChild(time);
      if (item.unread_count) {
        var badge = document.createElement('span'); badge.className = 'gm-msg-unread';
        badge.textContent = item.unread_count > 99 ? '99+' : String(item.unread_count); meta.appendChild(badge);
      }
      row.appendChild(meta); row.onclick = function () { openThread(item.id); }; list.appendChild(row);
    });
    var unread = state.conversations.reduce(function (n, item) { return n + (Number(item.unread_count) || 0); }, 0);
    if (total) total.textContent = unread ? unread + ' unread' : '';
    if (window.GMCommunity && window.GMCommunity.setMessageUnread) window.GMCommunity.setMessageUnread(unread);
  }
  function loadInbox() {
    var loading = $('gmMsgInboxLoading'), retry = $('gmMsgRetry'), empty = $('gmMsgEmpty');
    if (loading) loading.hidden = false; if (retry) retry.hidden = true;
    api('/api/messages/conversations')
      .then(function (data) { state.conversations = data.conversations || []; renderInbox(); })
      .catch(function (error) {
        if (empty) empty.hidden = true; if (retry) retry.hidden = false; toast(error.message);
      })
      .then(function () { if (loading) loading.hidden = true; });
  }
  function renderThread(data) {
    var conversation = data.conversation || {}; state.currentUser = conversation;
    var name = $('gmMsgThreadName'), host = $('gmMsgThreadAvatar'), log = $('gmMsgLog');
    if (name) name.textContent = '@' + escapeName(conversation.username);
    avatar(host, conversation); if (!log) return;
    log.textContent = '';
    (data.messages || []).forEach(function (message) {
      var bubble = document.createElement('div');
      bubble.className = 'gm-msg-bubble' + (message.is_me ? ' is-me' : '');
      var body = document.createElement('span'); body.textContent = message.body || ''; bubble.appendChild(body);
      var time = document.createElement('small'); time.className = 'gm-msg-time';
      time.textContent = timeAgo(message.created_at); bubble.appendChild(time); log.appendChild(bubble);
    });
    log.scrollTop = log.scrollHeight;
  }
  function markRead(id) { return api('/api/messages/conversations/' + encodeURIComponent(id) + '/read', { method: 'POST' }).catch(function () {}); }
  function showThreadError(error) {
    var node = $('gmMsgThreadError'); if (!node) return;
    node.textContent = error && error.message ? error.message : 'Could not load this conversation.';
    node.hidden = false;
  }
  function openThread(id) {
    if (!id) return;
    state.current = id; showThread();
    var log = $('gmMsgLog'), error = $('gmMsgThreadError');
    if (log) log.setAttribute('aria-busy', 'true'); if (error) error.hidden = true;
    api('/api/messages/conversations/' + encodeURIComponent(id))
      .then(function (data) { renderThread(data); return markRead(id); })
      .then(loadInbox).catch(showThreadError)
      .then(function () { if (log) log.setAttribute('aria-busy', 'false'); });
    startPolling();
  }
  function startConversation(username) {
    return api('/api/messages/start', { method: 'POST', body: JSON.stringify({ username: username }) })
      .then(function (data) {
        var id = data && data.conversation && data.conversation.id;
        if (!id) throw new Error('Could not open the conversation.');
        state.current = id; return id;
      });
  }
  function send(event) {
    event.preventDefault();
    var input = $('gmMsgInput'), sendButton = $('gmMsgSend');
    if (!input || !sendButton) return;
    var body = input.value.trim(); if (!body || sendButton.disabled) return;
    sendButton.disabled = true;
    var error = $('gmMsgThreadError'); if (error) error.hidden = true;
    var promise = state.current ? Promise.resolve(state.current) : startConversation(BOOT.initialUsername);
    promise.then(function (id) {
      return api('/api/messages/conversations/' + encodeURIComponent(id) + '/messages', {
        method: 'POST', body: JSON.stringify({ body: body, client_message_id: 'web-' + Date.now() })
      });
    }).then(function () {
      input.value = ''; return api('/api/messages/conversations/' + encodeURIComponent(state.current));
    }).then(renderThread).then(loadInbox).catch(showThreadError)
      .then(function () { sendButton.disabled = false; });
  }
  function startInitial() {
    if (!BOOT.initialUsername) return;
    showThread();
    var name = $('gmMsgThreadName'), status = $('gmMsgThreadStatus'), input = $('gmMsgInput');
    if (name) name.textContent = '@' + escapeName(BOOT.initialUsername);
    if (status) status.textContent = 'New conversation';
    avatar($('gmMsgThreadAvatar'), { username: BOOT.initialUsername });
    if (input && input.focus) input.focus();
  }
  function startPolling() {
    stopPolling(); state.timer = setInterval(function () {
      if (state.current) openThread(state.current); else loadInbox();
    }, 15000);
  }
  function stopPolling() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

  var back = $('gmMsgThreadBack'), retry = $('gmMsgRetry'), composer = $('gmMsgComposer');
  if (back) back.onclick = showInbox;
  if (retry) retry.onclick = loadInbox;
  if (composer) composer.onsubmit = send;
  startInitial(); if (!BOOT.initialUsername) loadInbox();
  window.GMCommunityMessages = { openThread: openThread, loadInbox: loadInbox };
}());
