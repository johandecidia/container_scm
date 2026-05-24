// Chat streaming event handlers
// Include this asset only on pages that have chat components

function handleWebSocketCommand(type: string, data: Record<string, string>) {
  switch (type) {
    case 'pushURL':
      window.history.pushState({}, '', data.url);
      break;
    case 'collapseThinking': {
      const el = document.querySelector(`#${data.targetId} [x-data]`);
      if (el) {
        const alpineData = (window as any).Alpine?.$data(el);
        if (alpineData) {
          alpineData.open = false;
          alpineData.done = true;
        }
      }
      break;
    }
    case 'hideThinking': {
      const thinkingEl = document.getElementById(data.targetId);
      if (thinkingEl) {
        thinkingEl.classList.add('hidden');
      }
      break;
    }
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initializeChatHandlers();
});

// Also initialize on HTMX swaps in case the chat is loaded dynamically
document.body.addEventListener('htmx:afterSwap', () => {
  initializeChatHandlers();
});

function initializeChatHandlers() {
  // Find chat components and attach handlers
  const chatWrappers = document.querySelectorAll('.pg-chat-wrapper');

  chatWrappers.forEach(wrapper => {
    // Skip if already initialized
    if (wrapper.hasAttribute('data-chat-initialized')) {
      return;
    }

    // Mark as initialized
    wrapper.setAttribute('data-chat-initialized', 'true');

    // Listen for websocket connection events to show better UI feedback to users
    wrapper.addEventListener('htmx:wsOpen', () => {
      // Open event causes connection status to show connected
      const alpineData = (window as any).Alpine?.$data(wrapper);
      if (alpineData) {
        alpineData.chatConnectionStatus = 'connected';
      }
    });

    wrapper.addEventListener('htmx:wsError', () => {
      // Error event causes connection status to show error
      const alpineData = (window as any).Alpine?.$data(wrapper);
      if (alpineData) {
        alpineData.chatConnectionStatus = 'error';
      }
    });

    wrapper.addEventListener('htmx:wsClose', () => {
      // Close event causes connection status to show error
      const alpineData = (window as any).Alpine?.$data(wrapper);
      if (alpineData) {
        alpineData.chatConnectionStatus = 'error';
      }
    });

    // Listen for websocket messages
    wrapper.addEventListener('htmx:wsAfterMessage', (evt: any) => {
      // Auto-scroll to bottom
      const messageList = wrapper.querySelector('#message-list');
      if (messageList) {
        messageList.scrollTop = messageList.scrollHeight;
      }

      // Handle JSON commands from the backend
      try {
        const message = JSON.parse(evt.detail.message);
        if (message.type) {
          handleWebSocketCommand(message.type, message.data);
        }
      } catch (e) {
        // Not JSON - likely a regular HTMX message, ignore
      }
    });
  });
}
