/* Private Community Messages. User text and filenames are always assigned with textContent. */
(function () {
  'use strict';
  var BOOT = window.GM_MESSAGES_BOOT || {};
  var state = { conversations: [], current: null, currentUser: null, timer: null, view: 'inbox', attachment: null };

  function $(id) { return document.getElementById(id); }
  function api(path, options) {
    var opts = options || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return fetch(path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) { var error = new Error(data.error || 'Request failed.'); error.data = data; throw error; }
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
  function formatBytes(value) {
    var bytes = Number(value) || 0;
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }
  function avatar(host, item) {
    if (!host) return;
    host.textContent = escapeName(item.username).charAt(0).toUpperCase();
    if (item.avatar_url) {
      var img = document.createElement('img');
      img.src = item.avatar_url; img.alt = ''; img.loading = 'lazy'; img.referrerPolicy = 'no-referrer';
      img.onerror = function () { img.remove(); };
      host.textContent = ''; host.appendChild(img);
    }
  }
  function toast(message) {
    var node = $('gmMsgToast'); if (!node) return;
    node.textContent = message; node.hidden = false; clearTimeout(node._timer);
    node._timer = setTimeout(function () { node.hidden = true; }, 2800);
  }
  function setView(view) {
    state.view = view === 'thread' ? 'thread' : 'inbox';
    var inbox = $('gmMsgInbox'), thread = $('gmMsgThread');
    var desktop = window.matchMedia && window.matchMedia('(min-width: 700px)').matches;
    [[inbox, state.view !== 'inbox' && !desktop], [thread, state.view !== 'thread']].forEach(function (pair) {
      if (!pair[0]) return;
      pair[0].hidden = pair[1]; pair[0].classList.toggle('is-hidden', pair[1]);
      pair[0].setAttribute('aria-hidden', pair[1] ? 'true' : 'false');
    });
    if (state.view === 'inbox') { state.current = null; stopPolling(); }
  }
  function showInbox() { setView('inbox'); loadInbox(); }
  function showThread() { setView('thread'); }
  function renderInbox() {
    var list = $('gmMsgList'), empty = $('gmMsgEmpty'), total = $('gmMsgTotal');
    if (!list || !empty) return;
    list.textContent = ''; empty.hidden = state.conversations.length !== 0;
    state.conversations.forEach(function (item) {
      var row = document.createElement('button'); row.type = 'button'; row.className = 'gm-msg-row';
      var image = document.createElement('span'); image.className = 'gm-msg-avatar'; avatar(image, item); row.appendChild(image);
      var main = document.createElement('span'); main.className = 'gm-msg-row-main';
      var name = document.createElement('span'); name.className = 'gm-msg-row-name'; name.textContent = '@' + escapeName(item.username); main.appendChild(name);
      var preview = document.createElement('span'); preview.className = 'gm-msg-row-preview';
      preview.textContent = item.last_message || 'Start a conversation'; main.appendChild(preview); row.appendChild(main);
      var meta = document.createElement('span'); meta.className = 'gm-msg-row-meta';
      var time = document.createElement('span'); time.textContent = timeAgo(item.last_message_at); meta.appendChild(time);
      if (item.unread_count) { var badge = document.createElement('span'); badge.className = 'gm-msg-unread'; badge.textContent = item.unread_count > 99 ? '99+' : String(item.unread_count); meta.appendChild(badge); }
      row.appendChild(meta); row.onclick = function () { openThread(item.id); }; list.appendChild(row);
    });
    var unread = state.conversations.reduce(function (n, item) { return n + (Number(item.unread_count) || 0); }, 0);
    if (total) total.textContent = unread ? unread + ' unread' : '';
    if (window.GMCommunity && window.GMCommunity.setMessageUnread) window.GMCommunity.setMessageUnread(unread);
  }
  function loadInbox() {
    var loading = $('gmMsgInboxLoading'), retry = $('gmMsgRetry'), empty = $('gmMsgEmpty');
    if (loading) loading.hidden = false; if (retry) retry.hidden = true;
    api('/api/messages/conversations').then(function (data) {
      state.conversations = data.conversations || []; renderInbox();
    }).catch(function (error) {
      if (empty) empty.hidden = true; if (retry) retry.hidden = false; toast(error.message);
    }).then(function () { if (loading) loading.hidden = true; });
  }
  function makeAttachmentNode(attachment) {
    if (!attachment) return null;
    var card = document.createElement('a'); card.className = 'gm-msg-bubble-attachment';
    card.href = attachment.download_url || '#'; card.target = '_blank'; card.rel = 'noopener';
    if (attachment.kind === 'image') {
      var img = document.createElement('img'); img.src = card.href; img.alt = attachment.name || 'Attached image'; img.loading = 'lazy'; card.appendChild(img);
    }
    var copy = document.createElement('span'); copy.className = 'gm-msg-file-card';
    var icon = document.createElement('b'); icon.textContent = attachment.kind === 'image' ? 'IMG' : 'PDF'; copy.appendChild(icon);
    var name = document.createElement('strong'); name.textContent = attachment.name || 'Attachment'; copy.appendChild(name);
    var size = document.createElement('small'); size.textContent = formatBytes(attachment.bytes); copy.appendChild(size);
    card.appendChild(copy); return card;
  }
  function renderThread(data) {
    var conversation = data.conversation || {}; state.currentUser = conversation;
    var name = $('gmMsgThreadName'), host = $('gmMsgThreadAvatar'), log = $('gmMsgLog');
    if (name) name.textContent = '@' + escapeName(conversation.username);
    avatar(host, conversation); if (!log) return;
    log.textContent = '';
    (data.messages || []).forEach(function (message) {
      var bubble = document.createElement('div'); bubble.className = 'gm-msg-bubble' + (message.is_me ? ' is-me' : '');
      if (message.body) { var body = document.createElement('span'); body.className = 'gm-msg-bubble-body'; body.textContent = message.body; bubble.appendChild(body); }
      var attachment = makeAttachmentNode(message.attachment); if (attachment) bubble.appendChild(attachment);
      var time = document.createElement('small'); time.className = 'gm-msg-time'; time.textContent = timeAgo(message.created_at); bubble.appendChild(time); log.appendChild(bubble);
    });
    log.scrollTop = log.scrollHeight;
  }
  function markRead(id) { return api('/api/messages/conversations/' + encodeURIComponent(id) + '/read', { method: 'POST' }).catch(function () {}); }
  function showThreadError(error) { var node = $('gmMsgThreadError'); if (!node) return; node.textContent = error && error.message ? error.message : 'Could not load this conversation.'; node.hidden = false; }
  function openThread(id) {
    if (!id) return;
    state.current = id; showThread();
    var log = $('gmMsgLog'), error = $('gmMsgThreadError');
    if (log) log.setAttribute('aria-busy', 'true'); if (error) error.hidden = true;
    api('/api/messages/conversations/' + encodeURIComponent(id)).then(function (data) { renderThread(data); return markRead(id); })
      .then(function () { if (state.view === 'thread') loadInbox(); }).catch(showThreadError)
      .then(function () { if (log) log.setAttribute('aria-busy', 'false'); });
    startPolling();
  }
  function startConversation(username) {
    return api('/api/messages/start', { method: 'POST', body: JSON.stringify({ username: username }) }).then(function (data) {
      var id = data && data.conversation && data.conversation.id; if (!id) throw new Error('Could not open the conversation.');
      state.current = id; return id;
    });
  }
  function setUploadUi(visible, name, status, error) {
    var card = $('gmMsgAttachment'), nameNode = $('gmMsgAttachmentName'), statusNode = $('gmMsgAttachmentStatus'), errorNode = $('gmMsgUploadError');
    if (card) card.hidden = !visible; if (nameNode) nameNode.textContent = name || ''; if (statusNode) statusNode.textContent = status || '';
    if (errorNode) { errorNode.textContent = error || ''; errorNode.hidden = !error; }
  }
  function uploadFile(file) {
    if (!file) return;
    if (file.size <= 0 || file.size > 10 * 1024 * 1024) { toast('Files must be between 1 byte and 10 MB.'); return; }
    state.attachment = { file: file, upload: null, pending: true };
    setUploadUi(true, file.name, 'Opening secure upload…', '');
    var ready = state.current ? Promise.resolve(state.current) : startConversation(BOOT.initialUsername);
    ready.then(function () {
      return api('/api/messages/conversations/' + encodeURIComponent(state.current) + '/attachments/presign', {
        method: 'POST', body: JSON.stringify({ name: file.name, mime: file.type, bytes: file.size })
      });
    }).then(function (signed) {
      state.attachment.upload = { upload_id: signed.upload_id, name: signed.name, mime: signed.mime, kind: signed.kind, bytes: file.size };
      setUploadUi(true, file.name, 'Uploading 0%', '');
      return fetch(signed.upload_url, { method: signed.method || 'PUT', headers: signed.headers || {}, body: file }).then(function (response) {
        if (!response.ok) throw new Error('File upload failed. Please retry.');
        state.attachment.pending = false; setUploadUi(true, file.name, 'Ready to send', '');
        showThread(); loadThreadAfterUpload();
      });
    }).catch(function (error) {
      if (!state.attachment) return;
      state.attachment.pending = false; state.attachment.error = true; setUploadUi(true, file.name, 'Upload failed', error.message || 'Upload failed.');
    });
  }
  function loadThreadAfterUpload() { if (!state.current) return; api('/api/messages/conversations/' + encodeURIComponent(state.current)).then(renderThread).catch(function () {}); }
  function removeAttachment(skipCancel) {
    var upload = state.attachment && state.attachment.upload;
    if (!skipCancel && upload && upload.upload_id) api('/api/messages/attachments/' + encodeURIComponent(upload.upload_id) + '/cancel', { method: 'POST' }).catch(function () {});
    state.attachment = null; setUploadUi(false, '', '', ''); var file = $('gmMsgFile'); if (file) file.value = '';
  }
  function send(event) {
    event.preventDefault();
    var input = $('gmMsgInput'), sendButton = $('gmMsgSend'); if (!input || !sendButton || sendButton.disabled) return;
    var body = input.value.trim(), attachment = state.attachment;
    if (!body && !attachment) { toast('Write a message or attach a file.'); return; }
    if (attachment && (attachment.pending || attachment.error || !attachment.upload)) { toast('Please wait for the file upload to finish.'); return; }
    sendButton.disabled = true; var error = $('gmMsgThreadError'); if (error) error.hidden = true;
    var promise = state.current ? Promise.resolve(state.current) : startConversation(BOOT.initialUsername);
    var payload = { body: body, client_message_id: 'web-' + Date.now() };
    if (attachment && attachment.upload) payload.attachment = { upload_id: attachment.upload.upload_id };
    promise.then(function (id) { return api('/api/messages/conversations/' + encodeURIComponent(id) + '/messages', { method: 'POST', body: JSON.stringify(payload) }); })
      .then(function () { input.value = ''; removeAttachment(true); return api('/api/messages/conversations/' + encodeURIComponent(state.current)); })
      .then(renderThread).then(loadInbox).catch(showThreadError).then(function () { sendButton.disabled = false; });
  }
  function startInitial() {
    if (!BOOT.initialUsername) return;
    state.current = null; showThread(); var name = $('gmMsgThreadName'), status = $('gmMsgThreadStatus'), input = $('gmMsgInput');
    if (name) name.textContent = '@' + escapeName(BOOT.initialUsername); if (status) status.textContent = 'New conversation';
    avatar($('gmMsgThreadAvatar'), { username: BOOT.initialUsername }); if (input && input.focus) input.focus();
  }
  function startPolling() {
    stopPolling(); state.timer = setInterval(function () { if (state.view === 'thread' && state.current) openThread(state.current); }, 15000);
  }
  function stopPolling() { if (state.timer) { clearInterval(state.timer); state.timer = null; } }

  var back = $('gmMsgThreadBack'), retry = $('gmMsgRetry'), composer = $('gmMsgComposer'), attach = $('gmMsgAttach'), file = $('gmMsgFile'), remove = $('gmMsgAttachmentRemove');
  if (back) back.onclick = showInbox; if (retry) retry.onclick = loadInbox; if (composer) composer.onsubmit = send;
  if (attach && file) attach.onclick = function () { file.click(); }; if (file) file.onchange = function () { uploadFile(file.files && file.files[0]); };
  if (remove) remove.onclick = removeAttachment;
  startInitial(); if (!BOOT.initialUsername) { showInbox(); }
  window.GMCommunityMessages = { openThread: openThread, loadInbox: loadInbox, setView: setView };
}());
