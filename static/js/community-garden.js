(function () {
    "use strict";

    var boot = window.GM_GARDEN_BOOT || {};
    var modal = document.getElementById("cfGardenModal");
    if (!modal || !boot.enabled || !boot.contractAddress || !boot.tokenAddress) return;

    var GARDEN_ABI = [
        "function goodDollarToken() view returns (address)",
        "function SEED_PRICE() view returns (uint256)",
        "function gardens(address) view returns (uint256 principal,uint64 plantedAt,uint16 seedCount,bool active)",
        "function maturityAt(address) view returns (uint256)",
        "function maximumBonus(address) view returns (uint256)",
        "function sponsorRewardPool() view returns (uint256)",
        "function buyAndPlant(uint256 seedCount)",
        "function harvestAndWithdraw() returns (uint256 principalReturned,uint256 sponsorBonus)",
        "function exitGarden() returns (uint256 principalReturned)"
    ];
    var TOKEN_ABI = [
        "function balanceOf(address account) view returns (uint256)",
        "function allowance(address owner,address spender) view returns (uint256)",
        "function approve(address spender,uint256 amount) returns (bool)"
    ];
    var _garden = null;
    var _token = null;
    var _ethers = null;
    var _seedPrice = 0n;
    var _busy = false;
    var _returnFocus = null;

    function el(id) { return document.getElementById(id); }
    function setText(id, value) {
        var node = el(id);
        if (node) node.textContent = String(value == null ? "" : value);
    }
    function setStatus(message, isError) {
        var node = el("cfGardenStatus");
        if (!node) return;
        node.textContent = String(message || "");
        node.classList.toggle("is-error", !!isError);
    }
    function walletAddress() {
        var address = window.GM_WALLET_BOOT && window.GM_WALLET_BOOT.wallet;
        if (!/^0x[a-fA-F0-9]{40}$/.test(String(address || ""))) {
            throw new Error("Your signed-in wallet is unavailable. Please sign in again.");
        }
        return address;
    }
    function formatGd(amount, digits) {
        var text = _ethers.formatUnits(amount, 18);
        var decimals = Number(typeof digits === "number" ? digits : 6);
        var parts = text.split(".");
        var whole = parts[0] || "0";
        if (!parts[1] || decimals <= 0) return whole;
        var fraction = parts[1].slice(0, decimals).replace(/0+$/, "");
        return fraction ? whole + "." + fraction : whole;
    }
    function money(amount) { return formatGd(amount) + " G$"; }
    function isActive(record) { return !!(record && record.active); }
    function maturityDate(seconds) {
        if (!Number(seconds)) return "—";
        return new Date(Number(seconds) * 1000).toLocaleString();
    }
    function setBusy(value) {
        _busy = !!value;
        ["cfGardenPlantButton", "cfGardenHarvestButton", "cfGardenExitButton", "cfGardenRefresh"].forEach(function (id) {
            var button = el(id);
            if (button) button.disabled = _busy;
        });
        updateSponsorButton();
        var card = modal.querySelector(".cf-garden-card");
        if (card) card.setAttribute("aria-busy", _busy ? "true" : "false");
    }
    function updateSponsorButton() {
        var button = el("cfGardenSponsorButton");
        var acknowledgement = el("cfGardenSponsorAck");
        if (button) button.disabled = _busy || !acknowledgement || !acknowledgement.checked;
    }
    function updateTotal() {
        var input = el("cfGardenSeedCount");
        if (!input) return;
        var count = Number(input.value);
        if (!Number.isInteger(count) || count < 1 || count > 100) {
            setText("cfGardenTotal", "Enter 1–100 seeds");
            return;
        }
        if (_seedPrice <= 0n) {
            setText("cfGardenTotal", "Reading contract price…");
            return;
        }
        setText("cfGardenTotal", money(BigInt(count) * _seedPrice));
    }

    async function contracts() {
        if (Number(boot.chainId) !== 42220) throw new Error("KwentoFi Garden is available on Celo Mainnet only.");
        if (!_ethers) {
            if (window.GMEthersLoader && typeof window.GMEthersLoader.load === "function") {
                _ethers = await window.GMEthersLoader.load();
            } else if (window.ethers) {
                _ethers = window.ethers;
            } else {
                throw new Error("Wallet tools are still loading. Please try again.");
            }
        }
        if (!window.P2PWallet || typeof window.P2PWallet.getReadProvider !== "function" || typeof window.P2PWallet.sendTx !== "function") {
            throw new Error("Celo wallet support is not ready. Please reload the Community page.");
        }
        // Tell the shared signing helper who is logged in. Without this the Garden
        // signed with empty defaults: a local (in-app) wallet fell through to the
        // WalletConnect bridge ("No active WalletConnect session.") and an
        // installed MetaMask extension could sign for an in-app account.
        window.P2PWallet.configure({
            wallet: walletAddress(),
            chainId: Number(boot.chainId) || 42220,
            loginMethod: (window.GM_WALLET_BOOT && window.GM_WALLET_BOOT.loginMethod) || "",
        });
        if (!_garden || !_token) {
            var provider = window.P2PWallet.getReadProvider();
            var gardenAddress = _ethers.getAddress(boot.contractAddress);
            var configuredTokenAddress = _ethers.getAddress(boot.tokenAddress);
            _garden = new _ethers.Contract(gardenAddress, GARDEN_ABI, provider);
            var deployedTokenAddress = _ethers.getAddress(await _garden.goodDollarToken());
            if (deployedTokenAddress.toLowerCase() !== configuredTokenAddress.toLowerCase()) {
                _garden = null;
                throw new Error("Garden token configuration does not match the deployed contract. Please contact support.");
            }
            _seedPrice = BigInt(await _garden.SEED_PRICE());
            if (_seedPrice <= 0n) {
                _garden = null;
                _seedPrice = 0n;
                throw new Error("The Garden contract returned an invalid seed price.");
            }
            _token = new _ethers.Contract(deployedTokenAddress, TOKEN_ABI, provider);
        }
        return { garden: _garden, token: _token, provider: window.P2PWallet };
    }

    async function refresh() {
        try {
            var wallet = walletAddress();
            var loaded = await contracts();
            setText("cfGardenSeedUnitPrice", money(_seedPrice));
            updateTotal();
            var values = await Promise.all([
                loaded.token.balanceOf(wallet),
                loaded.garden.sponsorRewardPool(),
                loaded.garden.gardens(wallet),
                loaded.garden.maturityAt(wallet),
                loaded.garden.maximumBonus(wallet)
            ]);
            setText("cfGardenBalance", money(values[0]));
            setText("cfGardenPool", money(values[1]));
            var record = values[2];
            var active = isActive(record);
            var plantPanel = el("cfGardenPlantPanel");
            var activePanel = el("cfGardenActivePanel");
            if (plantPanel) plantPanel.hidden = active;
            if (activePanel) activePanel.hidden = !active;
            if (!active) {
                setStatus("Choose 1–100 seeds. Each seed costs " + money(_seedPrice) + ". Your wallet will request approval only for the exact total if needed.");
                return;
            }
            setText("cfGardenPrincipal", money(record.principal));
            setText("cfGardenSeedCountValue", String(record.seedCount));
            setText("cfGardenMaturity", maturityDate(values[3]));
            setText("cfGardenBonus", money(values[4]) + " max, if pool remains");
            var chainTimestamp = Math.floor(Date.now() / 1000);
            try {
                var latestBlock = await loaded.garden.runner.provider.getBlock("latest");
                if (latestBlock && Number(latestBlock.timestamp)) chainTimestamp = Number(latestBlock.timestamp);
            } catch (_) { /* Display can fall back to the device clock if the public RPC is temporarily unavailable. */ }
            var mature = Number(values[3]) <= chainTimestamp;
            var harvestButton = el("cfGardenHarvestButton");
            var exitButton = el("cfGardenExitButton");
            if (harvestButton) harvestButton.hidden = !mature;
            if (exitButton) exitButton.hidden = mature;
            setStatus(mature
                ? "Your Garden is mature. Harvest returns principal; the optional bonus is limited to the available sponsor pool."
                : "Your Garden is growing. You may exit early to receive principal only, or wait until maturity to become eligible for an optional sponsor bonus.");
        } catch (error) {
            setStatus((error && error.message) || "Could not read Garden details. Check your connection and try again.", true);
        }
    }

    async function send(abi, methodArgs, to) {
        var loaded = await contracts();
        return loaded.provider.sendTx(to, abi, methodArgs, "0x0", { waitForReceipt: true });
    }

    async function plant() {
        if (_busy) return;
        var input = el("cfGardenSeedCount");
        var seeds = Number(input && input.value);
        if (!Number.isInteger(seeds) || seeds < 1 || seeds > 100) {
            setStatus("Choose a whole number of seeds from 1 to 100.", true);
            if (input) input.focus();
            return;
        }
        var amount;
        var address;
        try {
            address = walletAddress();
            setBusy(true);
            var loaded = await contracts();
            amount = BigInt(seeds) * _seedPrice;
            var balance = await loaded.token.balanceOf(address);
            if (balance < amount) throw new Error("Insufficient G$ balance for " + seeds + (seeds === 1 ? " seed." : " seeds."));
            var allowance = await loaded.token.allowance(address, boot.contractAddress);
            if (allowance < amount) {
                setStatus("Approve exactly " + money(amount) + " for this purchase in your Celo wallet.");
                await send("function approve(address spender,uint256 amount) returns (bool)", [boot.contractAddress, amount], boot.tokenAddress);
            }
            setStatus("Confirm the Garden purchase in your wallet. Your seeds will be planted after the transaction confirms.");
            await send("function buyAndPlant(uint256 seedCount)", [BigInt(seeds)], boot.contractAddress);
            await refresh();
            setStatus("Garden purchase submitted. Your 24-hour cycle begins when the transaction confirms on Celo.");
        } catch (error) {
            setStatus((error && error.message) || "Garden purchase failed or was cancelled.", true);
        } finally {
            setBusy(false);
        }
    }

    async function harvest() {
        if (_busy) return;
        try {
            setBusy(true);
            setStatus("Confirm harvest in your Celo wallet. The contract returns principal and any available sponsor-funded bonus.");
            await send("function harvestAndWithdraw() returns (uint256 principalReturned,uint256 sponsorBonus)", [], boot.contractAddress);
            await refresh();
            setStatus("Harvest transaction submitted. Check the wallet and refresh if the on-chain state takes a moment to update.");
        } catch (error) {
            setStatus((error && error.message) || "Harvest failed or was cancelled.", true);
        } finally {
            setBusy(false);
        }
    }

    async function exit() {
        if (_busy) return;
        try {
            setBusy(true);
            setStatus("Confirm early exit in your Celo wallet. This returns principal only; no bonus is paid.");
            await send("function exitGarden() returns (uint256 principalReturned)", [], boot.contractAddress);
            await refresh();
            setStatus("Exit transaction submitted. Your principal is returned when the transaction confirms; no bonus is paid on early exit.");
        } catch (error) {
            setStatus((error && error.message) || "Garden exit failed or was cancelled.", true);
        } finally {
            setBusy(false);
        }
    }

    async function sponsor() {
        if (_busy) return;
        var amountText = String((el("cfGardenSponsorAmount") || {}).value || "").trim();
        var acknowledgement = el("cfGardenSponsorAck");
        if (!acknowledgement || !acknowledgement.checked) {
            setStatus("Please confirm you understand sponsor contributions are irreversible.", true);
            return;
        }
        if (!/^\d{1,18}(?:\.\d{1,18})?$/.test(amountText)) {
            setStatus("Enter a positive G$ amount with no more than 18 decimal places.", true);
            return;
        }
        try {
            var loaded = await contracts();
            var amount = _ethers.parseUnits(amountText, 18);
            if (!amount || amount <= 0n) throw new Error("Contribution must be greater than zero.");
            var wallet = walletAddress();
            setBusy(true);
            var balance = await loaded.token.balanceOf(wallet);
            if (balance < amount) throw new Error("Insufficient G$ balance for this contribution.");
            var allowance = await loaded.token.allowance(wallet, boot.contractAddress);
            if (allowance < amount) {
                setStatus("Approve exactly " + amountText + " G$ for the sponsor contribution in your Celo wallet.");
                await send("function approve(address spender,uint256 amount) returns (bool)", [boot.contractAddress, amount], boot.tokenAddress);
            }
            setStatus("Confirm the irrevocable sponsor contribution in your Celo wallet.");
            await send("function fundSponsorPool(uint256 amount)", [amount], boot.contractAddress);
            if (el("cfGardenSponsorAmount")) el("cfGardenSponsorAmount").value = "";
            if (acknowledgement) acknowledgement.checked = false;
            updateSponsorButton();
            await refresh();
            setStatus("Sponsor contribution submitted. Thank you for helping fund optional Garden bonuses.");
        } catch (error) {
            setStatus((error && error.message) || "Sponsor contribution failed or was cancelled.", true);
        } finally {
            setBusy(false);
        }
    }

    function open(trigger) {
        if (window.GMCommunity && typeof window.GMCommunity.closeSheet === "function") {
            window.GMCommunity.closeSheet(false);
        }
        _returnFocus = trigger || document.getElementById("cfMenuGarden") || null;
        modal.hidden = false;
        modal.setAttribute("aria-hidden", "false");
        document.body.classList.add("cf-modal-open");
        var card = modal.querySelector(".cf-garden-card");
        if (card && card.focus) card.focus();
        setStatus("Loading your Celo Garden…");
        refresh();
        return true;
    }

    function close() {
        if (modal.hidden) return;
        modal.hidden = true;
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("cf-modal-open");
        var trigger = _returnFocus;
        _returnFocus = null;
        if (trigger && trigger.focus) trigger.focus();
    }

    var seedInput = el("cfGardenSeedCount");
    if (seedInput) seedInput.addEventListener("input", updateTotal);
    var sponsorAcknowledgement = el("cfGardenSponsorAck");
    if (sponsorAcknowledgement) sponsorAcknowledgement.addEventListener("change", updateSponsorButton);
    updateSponsorButton();
    document.addEventListener("keydown", function (event) {
        if (!modal.hidden && event.key === "Escape") close();
    });
    window.GMCommunityGarden = { open: open, close: close, refresh: refresh, plant: plant, harvest: harvest, exit: exit, sponsor: sponsor };
})();
