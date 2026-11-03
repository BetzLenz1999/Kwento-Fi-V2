/* In-page inbox and private conversation dialog for the Community feed. */
(function (window, document) {
  'use strict';

  var state = {
    open: false,
    view: 'inbox',
    conversations: [],
    current: null,
    opener: null,
    timer: null,
    generation: 0
  };

  function $(id) { return document.getElementById(id); }
  function boot() { return window.GM_COMMUNITY_BOOT || {}; }
  function modal() { return $('cfCommunityMessagesModal'); }

  function api(path, options) {
    var opts = options || {};
    opts.credentials = 'same-origin';
    opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    return window.fetch(path, opts).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          var error = new Error(data.error || data.message || 'Could not load messages.');
          error.code = data.code || '';
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function setUnreadCount(count) {
    if (window.GMCommunity && typeof window.GMCommunity.setMessageUnread === 'function') {
      window.GMCommunity.setMessageUnread(Math.max(0, Number(count) || 0));
    }
  }

  function setView(view) {
    state.view = view === 'thread' ? 'thread' : 'inbox';
    var inbox = $('cfCommunityMessagesInbox');
    var thread = $('cfCommunityMessagesThread');
    if (inbox) inbox.hidden = state.view !== 'inbox';
    if (thread) thread.hidden = state.view !== 'thread';
    if (state.view === 'inbox') state.current = null;
  }

  function avatar(host, user) {
    if (!host) return;
    var name = String((user && user.username) || 'Community member').replace(/^@+/, '');
    host.textContent = name.charAt(0).toUpperCase() || 'M';
    var url = user && user.avatar_url;
    if (!url || !(url.indexOf('/') === 0 || /^https:\/\//i.test(url))) return;
    var image = document.createElement('img');
    image.src = url;
    image.alt = '';
    image.loading = 'lazy';
    image.referrerPolicy = 'no-referrer';
    image.onerror = function () { image.remove(); };
    host.textContent = '';
    host.appendChild(image);
  }

  function timeAgo(value) {
    var stamp = new Date(value || '').getTime();
    if (!stamp) return '';
    var minutes = Math.max(0, Math.floor((Date.now() - stamp) / 60000));
    if (minutes < 1) return 'now';
    if (minutes < 60) return minutes + 'm';
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + 'h';
    return Math.floor(hours / 24) + 'd';
  }

  function showInboxError(message) {
    var error = $('cfCommunityMessagesInboxError');
    if (error) {
      error.textContent = message || 'Could not load conversations. Please try again.';
      error.hidden = false;
    }
  }

  function renderInbox() {
    var list = $('cfCommunityMessagesList');
    var empty = $('cfCommunityMessagesEmpty');
    var error = $('cfCommunityMessagesInboxError');
    var retry = $('cfCommunityMessagesRetry');
    if (!list || !empty) return;
    list.textContent = '';
    if (error) error.hidden = true;
    if (retry) retry.hidden = true;
    empty.hidden = state.conversations.length > 0;

    state.conversations.forEach(function (conversation) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'cf-community-messages-row';
      row.setAttribute('aria-label', 'Open conversation with @' + String(conversation.username || 'member').replace(/^@+/, ''));

      var image = document.createElement('span');
      image.className = 'cf-community-messages-avatar';
      avatar(image, conversation);
      row.appendChild(image);

      var copy = document.createElement('span');
      copy.className = 'cf-community-messages-row-copy';
      var name = document.createElement('strong');
      name.textContent = '@' + String(conversation.username || 'member').replace(/^@+/, '');
      copy.appendChild(name);
      var preview = document.createElement('span');
      preview.className = 'cf-community-messages-preview';
      preview.textContent = conversation.last_message || 'Start a conversation';
      copy.appendChild(preview);
      row.appendChild(copy);

      var meta = document.createElement('span');
      meta.className = 'cf-community-messages-meta';
      var time = document.createElement('span');
      time.textContent = timeAgo(conversation.last_message_at);
      meta.appendChild(time);
      if (Number(conversation.unread_count) > 0) {
        var unread = document.createElement('span');
        unread.className = 'cf-community-messages-unread';
        unread.textContent = Number(conversation.unread_count) > 99 ? '99+' : String(conversation.unread_count);
        meta.appendChild(unread);
      }
      row.appendChild(meta);
      row.addEventListener('click', function () { openThread(conversation.id); });
      list.appendChild(row);
    });

    var unreadConversations = state.conversations.reduce(function (total, item) {
      return total + (Number(item.unread_count) > 0 ? 1 : 0);
    }, 0);
    setUnreadCount(unreadConversations);
  }

  function loadInbox() {
    if (!state.open) return Promise.resolve(false);
    var token = state.generation;
    var loading = $('cfCommunityMessagesLoading');
    var error = $('cfCommunityMessagesInboxError');
    var retry = $('cfCommunityMessagesRetry');
    if (loading) loading.hidden = false;
    if (error) error.hidden = true;
    if (retry) retry.hidden = true;
    return api('/api/messages/conversations').then(function (data) {
      if (!state.open || token !== state.generation) return false;
      state.conversations = Array.isArray(data.conversations) ? data.conversations : [];
      renderInbox();
      return true;
    }).catch(function (err) {
      if (!state.open || token !== state.generation) return false;
      if (loading) loading.hidden = true;
      if ($('cfCommunityMessagesEmpty')) $('cfCommunityMessagesEmpty').hidden = true;
      showInboxError(err.message);
      if (retry) retry.hidden = false;
      return false;
    }).then(function (success) {
      if (state.open && token === state.generation && loading) loading.hidden = true;
      return success;
    });
  }

  function makeMessageNode(message) {
    var bubble = document.createElement('article');
    bubble.className = 'cf-community-messages-bubble' + (message.is_me ? ' is-mine' : '');
    if (message.body) {
      var body = document.createElement('p');
      body.className = 'cf-community-messages-bubble-text';
      body.textContent = message.body;
      bubble.appendChild(body);
    }
    if (message.transfer && message.transfer.type === 'onchain_transfer') {
      var transfer = document.createElement('div');
      transfer.className = 'cf-community-messages-transfer';
      var headline = document.createElement('strong');
      headline.textContent = 'On-chain transfer confirmed';
      transfer.appendChild(headline);
      var summary = document.createElement('p');
      summary.textContent = (message.transfer.amount || '') + ' ' +
        (message.transfer.token_label || message.transfer.token || '') +
        ' sent to @' + String(message.transfer.recipient_username || 'member').replace(/^@+/, '');
      transfer.appendChild(summary);
      if (/^https:\/\/celoscan\.io\/tx\//.test(message.transfer.explorer_url || '')) {
        var tx = document.createElement('a');
        tx.href = message.transfer.explorer_url;
        tx.target = '_blank';
        tx.rel = 'noopener noreferrer';
        tx.textContent = 'View transaction';
        transfer.appendChild(tx);
      }
      bubble.appendChild(transfer);
    }
    var attachment = message.attachment || {};
    if (attachment.download_url && /^\/api\/messages\/attachments\/[A-Za-z0-9_-]+\/download$/.test(attachment.download_url)) {
      var file = document.createElement('a');
      file.className = 'cf-community-messages-attachment';
      file.href = attachment.download_url;
      file.rel = 'nofollow';
      file.textContent = 'Attachment · ' + (attachment.name || 'Open file');
      bubble.appendChild(file);
    }
    var time = document.createElement('time');
    time.className = 'cf-community-messages-time';
    time.textContent = timeAgo(message.created_at);
    bubble.appendChild(time);
    return bubble;
  }

  function renderThread(data) {
    var conversation = data.conversation || {};
    var person = $('cfCommunityMessagesPerson');
    var host = $('cfCommunityMessagesAvatar');
    var log = $('cfCommunityMessagesLog');
    var error = $('cfCommunityMessagesThreadError');
    if (person) person.textContent = '@' + String(conversation.username || 'member').replace(/^@+/, '');
    avatar(host, conversation);
    if (error) error.hidden = true;
    if (!log) return;
    log.textContent = '';
    (data.messages || []).forEach(function (message) { log.appendChild(makeMessageNode(message)); });
    if (!data.messages || !data.messages.length) {
      var empty = document.createElement('p');
      empty.className = 'cf-community-messages-thread-empty';
      empty.textContent = 'This is the start of your private conversation.';
      log.appendChild(empty);
    }
    log.scrollTop = log.scrollHeight;
  }

  function showThreadError(message) {
    var error = $('cfCommunityMessagesThreadError');
    if (error) {
      error.textContent = message || 'Could not load this conversation. Please try again.';
      error.hidden = false;
    }
  }

  function loadThread(id, markRead) {
    var log = $('cfCommunityMessagesLog');
    var error = $('cfCommunityMessagesThreadError');
    if (error) error.hidden = true;
    if (log) {
      log.textContent = '';
      log.setAttribute('aria-busy', 'true');
      var status = document.createElement('p');
      status.className = 'cf-community-messages-thread-empty';
      status.textContent = 'Loading conversation…';
      log.appendChild(status);
    }
    return api('/api/messages/conversations/' + encodeURIComponent(id)).then(function (data) {
      if (!state.open || state.current !== id) return false;
      renderThread(data);
      var read = markRead
        ? api('/api/messages/conversations/' + encodeURIComponent(id) + '/read', { method: 'POST' }).catch(function () {})
        : Promise.resolve();
      return read.then(function () {
        if (state.open && state.current === id) loadInbox();
        return true;
      });
    }).catch(function (err) {
      if (state.open && state.current === id) showThreadError(err.message);
      return false;
    }).then(function (success) {
      if (log) log.setAttribute('aria-busy', 'false');
      return success;
    });
  }

  function openThread(id) {
    if (!id || !state.open) return;
    state.current = id;
    setView('thread');
    var send = $('cfCommunityMessagesSend');
    if (send) send.disabled = false;
    loadThread(id, true);
    startPolling();
    var input = $('cfCommunityMessagesInput');
    if (input && input.focus) input.focus();
  }

  function showInbox() {
    setView('inbox');
    loadInbox();
    var first = $('cfCommunityMessagesClose');
    if (first && first.focus) first.focus();
  }

  function sendMessage(event) {
    event.preventDefault();
    var input = $('cfCommunityMessagesInput');
    var button = $('cfCommunityMessagesSend');
    var body = input ? input.value.trim() : '';
    var id = state.current;
    if (!id || !body || !button || button.disabled) return;
    button.disabled = true;
    var clientId = window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID() : 'community-' + Date.now() + '-' + Math.random().toString(36).slice(2);
    api('/api/messages/conversations/' + encodeURIComponent(id) + '/messages', {
      method: 'POST',
      body: JSON.stringify({ body: body, client_message_id: clientId })
    }).then(function () {
      if (input) input.value = '';
      return loadThread(id, false);
    }).catch(function (err) {
      showThreadError(err.message);
    }).then(function () {
      if (button) button.disabled = false;
    });
  }

  function stopPolling() {
    if (state.timer) window.clearInterval(state.timer);
    state.timer = null;
  }

  function startPolling() {
    stopPolling();
    state.timer = window.setInterval(function () {
      if (!state.open || (document.visibilityState && document.visibilityState !== 'visible')) return;
      if (state.view === 'thread' && state.current) loadThread(state.current, true);
      else loadInbox();
    }, 60000);
  }

  function close(restoreFocus) {
    var dialog = modal();
    if (!dialog || !state.open) return;
    state.open = false;
    state.generation += 1;
    stopPolling();
    dialog.hidden = true;
    dialog.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('cf-community-messages-open');
    state.current = null;
    var opener = state.opener;
    state.opener = null;
    if (restoreFocus !== false && opener && opener.focus) opener.focus();
  }

  function open(opener) {
    var dialog = modal();
    if (!dialog) return false;
    if (state.open) {
      loadInbox();
      return true;
    }
    state.open = true;
    state.generation += 1;
    state.opener = opener || document.activeElement;
    dialog.hidden = false;
    dialog.setAttribute('aria-hidden', 'false');
    document.body.classList.add('cf-community-messages-open');
    setView('inbox');
    state.conversations = [];
    var list = $('cfCommunityMessagesList');
    var emptyState = $('cfCommunityMessagesEmpty');
    var inboxError = $('cfCommunityMessagesInboxError');
    var retry = $('cfCommunityMessagesRetry');
    var loadingState = $('cfCommunityMessagesLoading');
    if (list) list.textContent = '';
    if (emptyState) emptyState.hidden = true;
    if (inboxError) inboxError.hidden = true;
    if (retry) retry.hidden = true;
    if (loadingState) loadingState.hidden = false;
    var closeButton = $('cfCommunityMessagesClose');
    if (closeButton && closeButton.focus) closeButton.focus();
    if (!boot().messagesEnabled) {
      if (loadingState) loadingState.hidden = true;
      showInboxError('Community messaging is temporarily unavailable. Please try again later.');
      return true;
    }
    loadInbox();
    startPolling();
    return true;
  }

  function bind() {
    var dialog = modal();
    if (!dialog) return;
    var closeButton = $('cfCommunityMessagesClose');
    var back = $('cfCommunityMessagesBack');
    var retry = $('cfCommunityMessagesRetry');
    var composer = $('cfCommunityMessagesComposer');
    if (closeButton) closeButton.addEventListener('click', function () { close(); });
    if (back) back.addEventListener('click', showInbox);
    if (retry) retry.addEventListener('click', loadInbox);
    if (composer) composer.addEventListener('submit', sendMessage);
    dialog.addEventListener('click', function (event) {
      if (event.target === dialog) close();
    });
    document.addEventListener('keydown', function (event) {
      if (!state.open) return;
      if (event.key === 'Escape') {
        close();
        return;
      }
      if (event.key !== 'Tab') return;
      var focusable = dialog.querySelectorAll('button:not(:disabled), textarea:not(:disabled), a[href]');
      if (!focusable.length) return;
      var first = focusable[0];
      var last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    document.addEventListener('visibilitychange', function () {
      if (!state.open || !state.current || (document.visibilityState && document.visibilityState !== 'visible')) return;
      loadThread(state.current, true);
    });
  }

  bind();
  window.GMCommunityMessageModal = { open: open, close: close, loadInbox: loadInbox, openThread: openThread };
}(window, document));
