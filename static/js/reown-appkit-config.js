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
        const client = this.modal && this.modal.adapter && this.modal.adapter.connectionControllerClient;
        if (client && typeof client.disconnect === 'function') return client.disconnect();
        return Promise.resolve();
    },
    getProvider() {
        return this.modal ? this.modal.getWalletProvider() : null;
    },
    getAddress() {
        return this.modal ? this.modal.getAddress() : undefined;
    },
    getWalletProviderType() {
        return this.modal ? this.modal.getWalletProviderType() : '';
    },
    getIsConnected() {
        return this.modal ? this.modal.getIsConnected() : false;
    },
    subscribeProvider(callback) {
        if (!this.modal) return function () {};
        return this.modal.subscribeProvider(callback);
    },
    waitForProvider(timeoutMs = 30000) {
        if (!this.modal) {
            return Promise.reject(new Error(this.error || 'Reown AppKit is not configured.'));
        }
        const modal = this.modal;
        const current = modal.getWalletProvider();
        if (modal.getIsConnected() && current && typeof current.request === 'function') {
            return Promise.resolve(current);
        }
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
            unsubscribe = modal.subscribeProvider((state = {}) => {
                const provider = state.provider || modal.getWalletProvider();
                if (state.isConnected && provider && typeof provider.request === 'function') {
                    finish(null, provider);
                }
            });
            if (settled && typeof unsubscribe === 'function') unsubscribe();
            const restored = modal.getWalletProvider();
            if (modal.getIsConnected() && restored && typeof restored.request === 'function') {
                finish(null, restored);
            }
        });
    },
    connect() {
        if (!this.modal) {
            throw new Error(this.error || 'Reown AppKit is not configured.');
        }
        const modal = this.modal;
        const currentProvider = modal.getWalletProvider();
        if (modal.getIsConnected() && currentProvider && typeof currentProvider.request === 'function') {
            return Promise.resolve({
                provider: currentProvider,
                providerType: modal.getWalletProviderType() || ''
            });
        }
        return new Promise((resolve, reject) => {
            let settled = false;
            let sawOpen = false;
            let providerUnsubscribe = null;
            let stateUnsubscribe = null;
            const timer = window.setTimeout(() => finish(new Error('Wallet connection timed out. Please try again.')), 120000);
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
            providerUnsubscribe = modal.subscribeProvider((state = {}) => {
                if (!state.isConnected) return;
                const provider = state.provider || modal.getWalletProvider();
                if (!provider || typeof provider.request !== 'function') {
                    finish(new Error('Connected wallet did not provide an EIP-1193 provider.'));
                    return;
                }
                finish(null, {
                    provider,
                    providerType: state.providerType || modal.getWalletProviderType() || ''
                });
            });
            if (settled && typeof providerUnsubscribe === 'function') providerUnsubscribe();
            if (typeof modal.subscribeState === 'function' && !settled) {
                stateUnsubscribe = modal.subscribeState((state = {}) => {
                    if (state.open) sawOpen = true;
                    else if (sawOpen) finish(new Error('Wallet connection was cancelled.'));
                });
                if (settled && typeof stateUnsubscribe === 'function') stateUnsubscribe();
            }
            if (settled) return;
            try {
                modal.open({ view: 'Connect', namespace: 'eip155' });
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
        allWallets: 'SHOW',
        themeMode: 'dark'
    });
}

window.GMReownAppKit = api;
