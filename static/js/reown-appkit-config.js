import { createAppKit } from '@reown/appkit';
import { EthersAdapter } from '@reown/appkit-adapter-ethers';
import { celo, defineChain } from '@reown/appkit/networks';

const boot = window.GM_REOWN_APPKIT_BOOT || window.GM_GET_STARTED_BOOT || {};
const projectId = String(boot.projectId || '').trim();

const xdc = defineChain({
    id: 50,
    caipNetworkId: 'eip155:50',
    chainNamespace: 'eip155',
    name: 'XDC Network',
    nativeCurrency: { name: 'XDC', symbol: 'XDC', decimals: 18 },
    rpcUrls: { default: { http: ['https://erpc.xinfin.network'] } },
    blockExplorers: {
        default: { name: 'XDCScan', url: 'https://xdcscan.io' }
    }
});

function readEvmAccount(modal) {
    if (!modal || typeof modal.getAccount !== 'function') return null;
    try {
        return modal.getAccount('eip155') || null;
    } catch (error) {
        console.warn('[Reown AppKit] Could not read EVM account state:', error);
        return null;
    }
}

function readProviderState(modal) {
    const account = readEvmAccount(modal);
    const rawProvider = modal && typeof modal.getWalletProvider === 'function'
        ? modal.getWalletProvider()
        : null;
    return {
        account,
        provider: rawProvider && typeof rawProvider.request === 'function' ? rawProvider : null,
        providerType: modal && typeof modal.getWalletProviderType === 'function'
            ? (modal.getWalletProviderType() || '')
            : '',
        isConnected: !!(account && account.isConnected)
    };
}

function subscribeProviderState(modal, callback) {
    const subscriptions = [];
    const publish = () => callback(readProviderState(modal));
    if (typeof modal.subscribeAccount === 'function') {
        subscriptions.push(modal.subscribeAccount(publish, 'eip155'));
    }
    if (typeof modal.subscribeProviders === 'function') {
        subscriptions.push(modal.subscribeProviders(publish));
    }
    return function unsubscribe() {
        subscriptions.forEach((unsubscribeOne) => {
            if (typeof unsubscribeOne === 'function') unsubscribeOne();
        });
    };
}

const api = {
    modal: null,
    error: null,
    open() {
        if (!this.modal) {
            throw new Error(this.error || 'Reown AppKit is not configured.');
        }
        this.modal.open({ view: 'Connect', namespace: 'eip155' });
    },
    close() {
        if (this.modal) this.modal.close();
    },
    disconnect() {
        if (this.modal && typeof this.modal.disconnect === 'function') {
            return this.modal.disconnect({ namespace: 'eip155' });
        }
        return Promise.resolve();
    },
    getProvider() {
        return readProviderState(this.modal).provider;
    },
    getAddress() {
        const account = readEvmAccount(this.modal);
        return account && account.address ? account.address : undefined;
    },
    getWalletProviderType() {
        return readProviderState(this.modal).providerType;
    },
    getIsConnected() {
        return readProviderState(this.modal).isConnected;
    },
    subscribeProvider(callback) {
        if (!this.modal) return function () {};
        return subscribeProviderState(this.modal, callback);
    },
    waitForProvider(timeoutMs = 30000) {
        if (!this.modal) {
            return Promise.reject(new Error(this.error || 'Reown AppKit is not configured.'));
        }
        const modal = this.modal;
        const current = readProviderState(modal);
        if (current.isConnected && current.provider) return Promise.resolve(current.provider);
        return new Promise((resolve, reject) => {
            let settled = false;
            let unsubscribe = null;
            const finish = (error, provider) => {
                if (settled) return;
                settled = true;
                window.clearTimeout(timer);
                if (typeof unsubscribe === 'function') unsubscribe();
                if (error) reject(error);
                else resolve(provider);
            };
            const timer = window.setTimeout(
                () => finish(new Error('Reown AppKit wallet session is not ready.')),
                Math.max(1000, Number(timeoutMs) || 30000)
            );
            unsubscribe = subscribeProviderState(modal, (state) => {
                if (state.isConnected && state.provider) finish(null, state.provider);
            });
            const restored = readProviderState(modal);
            if (restored.isConnected && restored.provider) finish(null, restored.provider);
        });
    },
    connect({ onModalOpen, onModalClose } = {}) {
        if (!this.modal) {
            throw new Error(this.error || 'Reown AppKit is not configured.');
        }
        const modal = this.modal;
        const current = readProviderState(modal);
        if (current.isConnected && current.provider) {
            return Promise.resolve({ provider: current.provider, providerType: current.providerType });
        }
        return new Promise((resolve, reject) => {
            let settled = false;
            let sawOpen = false;
            let sawClose = false;
            let providerUnsubscribe = null;
            let stateUnsubscribe = null;
            const cleanup = () => {
                window.clearTimeout(timer);
                if (typeof providerUnsubscribe === 'function') providerUnsubscribe();
                if (typeof stateUnsubscribe === 'function') stateUnsubscribe();
            };
            const finish = (error, connection) => {
                if (settled) return;
                settled = true;
                cleanup();
                if (error) reject(error);
                else resolve(connection);
            };
            const acceptConnection = (state) => {
                if (state && state.isConnected && state.provider) {
                    finish(null, { provider: state.provider, providerType: state.providerType || '' });
                }
            };
            const timer = window.setTimeout(
                () => finish(new Error(
                    sawClose
                        ? 'Wallet connection timed out. If your wallet app opened, approve the request there and return to this page before trying again.'
                        : 'Wallet connection timed out. Please try again.'
                )),
                120000
            );
            providerUnsubscribe = subscribeProviderState(modal, acceptConnection);
            if (typeof modal.subscribeState === 'function') {
                stateUnsubscribe = modal.subscribeState((state = {}) => {
                    if (state.open) sawOpen = true;
                    else if (sawOpen) {
                        // WalletConnect mobile handoff can close the picker while
                        // Trust Wallet / MiniPay is opening. Closing the picker is
                        // not proof that the user cancelled; keep listening for the
                        // provider/session to become ready (up to the connect timeout).
                        sawOpen = false;
                        sawClose = true;
                        if (typeof onModalClose === 'function') onModalClose();
                    }
                });
            }
            acceptConnection(readProviderState(modal));
            if (settled) return;
            try {
                Promise.resolve(modal.open({ view: 'Connect', namespace: 'eip155' }))
                    .then(() => {
                        if (!settled && !sawClose && typeof onModalOpen === 'function') onModalOpen();
                    })
                    .catch((error) => finish(error));
            } catch (error) {
                finish(error);
            }
        });
    }
};

if (!projectId) {
    api.error = 'Missing Reown project ID (walletconnect_project_id).';
    console.error('[Reown AppKit]', api.error);
} else {
    const origin = window.location.origin;
    api.modal = createAppKit({
        adapters: [new EthersAdapter()],
        networks: [celo, xdc],
        defaultNetwork: celo,
        projectId,
        metadata: {
            name: 'Kwento-Fi',
            description: 'Kwento-Fi wallet connection',
            url: origin,
            icons: []
        },
        features: {
            email: false,
            socials: [],
            analytics: false
        },
        enableWalletGuide: false,
        enableReconnect: true,
        // Use WalletConnect for external wallet sessions instead of letting
        // AppKit also register the page's injected/EIP-6963 providers. The
        // Get Started provider scan remains advisory only; it does not start
        // a competing wallet request.
        enableInjected: false,
        enableEIP6963: false,
        allWallets: 'SHOW',
        // The homepage Get Started overlay is at z-index 10000. AppKit's
        // default 9999 would leave its picker hidden behind that full-screen
        // overlay, so keep the actual wallet picker above it.
        themeVariables: { '--apkt-z-index': 10001 },
        themeMode: 'dark'
    });
}

window.GMReownAppKit = api;
