(function () {
    "use strict";

    var boot = window.GM_GARDEN_BOOT || {};
    var modal = document.getElementById("cfGardenModal");
    if (!modal || !boot.enabled || !boot.tokenAddress || (!boot.v2Enabled && !boot.legacyEnabled)) return;

    /* Garden is verified-only, like Swap and Reloadly: buying seeds spends G$
       and earns a share of the reward pool, so an unverified (sybil) wallet
       must not take part. The gate is UI-level because planting is a direct
       contract call from this bundle — there is no server endpoint to refuse.
       `fv_verified` comes from the shared wallet boot payload. Deliberately
       hard-coded: there is no env flag that can turn this off. */
    function fvVerified() {
        var walletBoot = window.GM_WALLET_BOOT || {};
        return walletBoot.fvVerified === true;
    }

    function requireFaceVerification() {
        if (window.GMCommunity && typeof window.GMCommunity.closeSheet === "function") {
            window.GMCommunity.closeSheet(false);
        }
        window._walletNeedsFV = true;
        if (window._walletFvReason !== "re_verification_needed") {
            window._walletFvReason = "not_verified";
        }
        if (typeof window.openClaimOrVerify === "function") {
            window.openClaimOrVerify();
            return true;
        }
        if (window.GMCommunity && typeof window.GMCommunity.openClaim === "function") {
            window.GMCommunity.openClaim();
        }
        return true;
    }

    /* Defense in depth: every signing action is reachable as a bare export
       (window.GMCommunityGarden.plant()), so gating only open() would let a
       console call still sign. Each money-moving action re-checks first. */
    function fvBlocked(action) {
        if (fvVerified()) return false;
        requireFaceVerification();
        setStatus("Verify your identity to " + action + ".", true);
        return true;
    }

    var V2_GARDEN_ABI = [
        "function goodDollarToken() view returns (address)",
        "function SEED_PRICE() view returns (uint256)",
        "function seedPrice() view returns (uint256)",
        "function plantingPaused() view returns (bool)",
        "function MAX_SEEDS() view returns (uint256)",
        "function maxSeeds() view returns (uint16)",
        "function gardens(address) view returns (uint256 principal,uint16 bonusBpsAtPlant,uint64 plantedAt,uint16 seedCount,bool active)",
        "function maturityAt(address) view returns (uint256)",
        "function maximumBonus(address) view returns (uint256)",
        "function buyAndPlant(uint256 seedCount)",
        "function harvestAndWithdraw() returns (uint256 principalReturned,uint256 sponsorBonus)",
        "function exitGarden() returns (uint256 principalReturned)"
    ];
    var V1_GARDEN_ABI = [
        "function goodDollarToken() view returns (address)",
        "function gardens(address) view returns (uint256 principal,uint64 plantedAt,uint16 seedCount,bool active)",
        "function maturityAt(address) view returns (uint256)",
        "function maximumBonus(address) view returns (uint256)",
        "function harvestAndWithdraw() returns (uint256 principalReturned,uint256 sponsorBonus)",
        "function exitGarden() returns (uint256 principalReturned)"
    ];
    var TOKEN_ABI = [
        "function balanceOf(address account) view returns (uint256)",
        "function allowance(address owner,address spender) view returns (uint256)",
        "function approve(address spender,uint256 amount) returns (bool)"
    ];
    var _v2 = null;
    var _v1 = null;
    var _token = null;
    var _positions = {};
    var _selectedVersion = "v1";
    var _ethers = null;
    var _seedPrice = 0n;
    var _maxSeeds = Number(boot.maxSeeds) || 100;
    var _gasRequestId = null;
    var _gasRequestSeeds = null;
    var _busy = false;
    var _returnFocus = null;
    var _growthInterval = null;
    var _plantedAt = 0;
    var _selectedGarden = null;
    var _maturityAt = 0;
    var _chainTimeOffset = 0;
    var _plantingPaused = false;
    var _pendingSponsorTxHash = null;

    function el(id) { return document.getElementById(id); }
    function setText(id, value) {
        var node = el(id);
        if (node) node.textContent = String(value == null ? "" : value);
    }
    function setStatus(message, isError) {
        var node = el("cfGardenStatus");
        if (node) {
            node.textContent = String(message || "");
            node.classList.toggle("is-error", !!isError);
        }
        var processing = el("cfGardenProcessing");
        var processingMessage = el("cfGardenProcessingMessage");
        if (processing && !processing.hidden && processingMessage) {
            processingMessage.textContent = String(message || "");
        }
    }
    function showGardenProcessing(title, message) {
        var processing = el("cfGardenProcessing");
        if (!processing) return;
        setText("cfGardenProcessingTitle", title || "Preparing your Garden");
        processing.hidden = false;
        processing.setAttribute("aria-hidden", "false");
        setStatus(message || "Processing Garden gas support…");
    }
    function hideGardenProcessing() {
        var processing = el("cfGardenProcessing");
        if (!processing) return;
        processing.hidden = true;
        processing.setAttribute("aria-hidden", "true");
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
    function formatDuration(seconds) {
        var remaining = Math.max(0, Math.floor(Number(seconds) || 0));
        var hours = Math.floor(remaining / 3600);
        var minutes = Math.floor((remaining % 3600) / 60);
        if (hours > 0) return hours + "h " + String(minutes).padStart(2, "0") + "m until harvest";
        return minutes + "m until harvest";
    }
    function renderGrowth(plantedAt, maturityAt, currentTime) {
        var plot = el("cfGardenPlot");
        if (!plot || !plantedAt || !maturityAt) return;
        var now = Number.isFinite(Number(currentTime)) ? Number(currentTime) : Math.floor(Date.now() / 1000) + _chainTimeOffset;
        var total = Math.max(1, maturityAt - plantedAt);
        var progress = Math.max(0, Math.min(1, (now - plantedAt) / total));
        var stage = progress >= 1 ? 6 : Math.min(5, Math.floor(progress * 6));
        var stages = [
            ["Seed tucked into the soil", "Your seed is in the soil. Come back as it starts to sprout."],
            ["A tiny sprout is breaking through", "The first green shoots are reaching for the light."],
            ["Leaves are unfolding", "Your plant is settling in and growing stronger."],
            ["The plant is flowering", "A blossom is forming. The vegetable is on its way."],
            ["Your vegetable is taking shape", "The harvest is getting fuller with every passing hour."],
            ["Almost ready to harvest", "One last growth stage remains before your Garden matures."],
            ["Mature vegetable ready", "Your Garden is mature. Harvest your principal and any available bonus."]
        ];
        plot.dataset.stage = String(stage);
        setText("cfGardenStageLabel", stages[stage][0]);
        setText("cfGardenStageHint", stages[stage][1]);
        setText("cfGardenActiveTitle", progress >= 1 ? "Your Garden is ready" : "Your Garden is growing");
        setText("cfGardenGrowthStage", "Stage " + (stage + 1) + " of 7");
        setText("cfGardenGrowthPercent", Math.round(progress * 100) + "%");
        setText("cfGardenGrowthCountdown", progress >= 1 ? "Ready to harvest" : formatDuration(maturityAt - now));
        var bar = el("cfGardenGrowthBar");
        if (bar) bar.style.width = Math.round(progress * 100) + "%";
        var progressBar = plot.parentElement && plot.parentElement.querySelector('[role="progressbar"]');
        if (progressBar) progressBar.setAttribute("aria-valuenow", String(Math.round(progress * 100)));
        var markers = modal.querySelectorAll(".cf-garden-timeline span");
        markers.forEach(function (marker, index) {
            marker.classList.toggle("is-done", index < stage);
            marker.classList.toggle("is-current", index === stage);
        });
    }
    function startGrowthTicker() {
        if (_growthInterval) window.clearInterval(_growthInterval);
        _growthInterval = window.setInterval(function () {
            if (!_busy && !modal.hidden && _plantedAt && _maturityAt) renderGrowth(_plantedAt, _maturityAt);
        }, 60000);
    }
    function stopGrowthTicker() {
        if (_growthInterval) window.clearInterval(_growthInterval);
        _growthInterval = null;
    }
    function setBusy(value) {
        _busy = !!value;
        ["cfGardenPlantButton", "cfGardenHarvestButton", "cfGardenExitButton", "cfGardenRefresh", "cfGardenPositionSelect"].forEach(function (id) {
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
        if (!Number.isInteger(count) || count < 1 || count > _maxSeeds) {
            setText("cfGardenTotal", "Enter 1–" + _maxSeeds + " seeds");
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
            if (window.GMEthersLoader && typeof window.GMEthersLoader.load === "function") _ethers = await window.GMEthersLoader.load();
            else if (window.ethers) _ethers = window.ethers;
            else throw new Error("Wallet tools are still loading. Please try again.");
        }
        if (!window.P2PWallet || typeof window.P2PWallet.getReadProvider !== "function" || typeof window.P2PWallet.sendTx !== "function") throw new Error("Celo wallet support is not ready. Please reload the Community page.");
        window.P2PWallet.configure({ wallet: walletAddress(), chainId: Number(boot.chainId) || 42220, loginMethod: (window.GM_WALLET_BOOT && window.GM_WALLET_BOOT.loginMethod) || "" });
        var provider = window.P2PWallet.getReadProvider();
        var configuredTokenAddress = _ethers.getAddress(boot.tokenAddress);
        async function checkedContract(address, abi) {
            var c = new _ethers.Contract(_ethers.getAddress(address), abi, provider);
            var deployedTokenAddress = _ethers.getAddress(await c.goodDollarToken());
            if (deployedTokenAddress.toLowerCase() !== configuredTokenAddress.toLowerCase()) throw new Error("Garden token configuration does not match the deployed contract. Please contact support.");
            return c;
        }
        if (boot.v2Enabled && !_v2) {
            if (!boot.contractAddress) throw new Error("Garden V2 is enabled but its contract address is missing.");
            _v2 = await checkedContract(boot.contractAddress, V2_GARDEN_ABI);
            _token = new _ethers.Contract(configuredTokenAddress, TOKEN_ABI, provider);
        }
        if (_v2) {
            try { _seedPrice = BigInt(await _v2.seedPrice()); } catch (_) { _seedPrice = BigInt(await _v2.SEED_PRICE()); }
            try { _maxSeeds = Number(await _v2.maxSeeds()); } catch (_) { _maxSeeds = Number(await _v2.MAX_SEEDS()); }
            _plantingPaused = await _v2.plantingPaused();
            if (!Number.isInteger(_maxSeeds) || _maxSeeds < 1 || _maxSeeds > 1000 || _seedPrice <= 0n) throw new Error("The Garden V2 contract returned invalid planting settings.");
            setText("cfGardenSeedUnitPrice", money(_seedPrice));
            var input = el("cfGardenSeedCount"); if (input) input.max = String(_maxSeeds);
            var limit = modal.querySelector(".cf-garden-limit"); if (limit) limit.textContent = "1–" + _maxSeeds + " seeds";
            updateTotal();
        }
        if (boot.legacyEnabled && !_v1) {
            if (!boot.legacyContractAddress) throw new Error("Legacy Garden is enabled but its contract address is missing.");
            _v1 = await checkedContract(boot.legacyContractAddress, V1_GARDEN_ABI);
        }
        if (!_token) _token = new _ethers.Contract(configuredTokenAddress, TOKEN_ABI, provider);
        return { v2: _v2, v1: _v1, token: _token, provider: window.P2PWallet };
    }

    async function refresh() {
        try {
            var wallet = walletAddress();
            var loaded = await contracts();
            var balance = await loaded.token.balanceOf(wallet);
            setText("cfGardenBalance", money(balance));
            _positions = {};
            async function readPosition(version, contract) {
                if (!contract) return;
                var vals = await Promise.all([contract.gardens(wallet), contract.maturityAt(wallet), contract.maximumBonus(wallet)]);
                var r = vals[0];
                _positions[version] = { version: version, contract: contract, address: version === "v1" ? boot.legacyContractAddress : boot.contractAddress, record: r, maturity: Number(vals[1]), bonus: vals[2], active: isActive(r) };
            }
            // Read each deployment independently; failure of a configured legacy deployment fails closed.
            if (boot.legacyEnabled) await readPosition("v1", loaded.v1);
            if (boot.v2Enabled) await readPosition("v2", loaded.v2);
            var v1 = _positions.v1, v2 = _positions.v2;
            var choices = [v1, v2].filter(function (x) { return x && x.active; });
            var selector = el("cfGardenPositionSelect");
            if (selector) {
                selector.hidden = choices.length < 2;
                selector.innerHTML = "";
                choices.forEach(function (x) { var o = document.createElement("option"); o.value = x.version; o.textContent = x.version === "v1" ? "Legacy Garden (V1)" : "Current Garden (V2)"; selector.appendChild(o); });
                if (choices.some(function (x) { return x.version === _selectedVersion; })) selector.value = _selectedVersion;
                else { _selectedVersion = choices[0] ? choices[0].version : "v1"; selector.value = _selectedVersion; }
            }
            var position = choices.find(function (x) { return x.version === _selectedVersion; }) || choices[0];
            var activePanel = el("cfGardenActivePanel"), plantPanel = el("cfGardenPlantPanel");
            if (activePanel) activePanel.hidden = !position;
            var legacyUnreadable = !!(boot.legacyEnabled && !v1);
            var canPlant = !!(boot.v2Enabled && v2 && !legacyUnreadable && !choices.length && !_plantingPaused);
            if (plantPanel) plantPanel.hidden = !canPlant;
            var sponsorPanel = modal.querySelector(".cf-garden-sponsor-panel"); if (sponsorPanel) sponsorPanel.hidden = !boot.v2Enabled;
            if (position) {
                _selectedVersion = position.version; _selectedGarden = position;
                setText("cfGardenTitle", position.version === "v1" ? "Your Legacy Garden" : "Your Garden");
                setText("cfGardenPositionVersion", position.version === "v1" ? "Legacy deployment · V1" : "Current deployment · V2");
                var r = position.record; _plantedAt = Number(r.plantedAt); _maturityAt = position.maturity;
                setText("cfGardenPrincipal", money(r.principal)); setText("cfGardenSeedCountValue", String(r.seedCount));
                setText("cfGardenMaturity", maturityDate(position.maturity)); setText("cfGardenBonus", money(position.bonus) + " maximum, subject to available funding");
                var timestamp = Math.floor(Date.now()/1000);
                try { var block = await position.contract.runner.provider.getBlock("latest"); if (block && Number(block.timestamp)) timestamp = Number(block.timestamp); } catch (_) {}
                _chainTimeOffset = timestamp - Math.floor(Date.now()/1000); renderGrowth(_plantedAt, _maturityAt, timestamp); startGrowthTicker();
                var mature = position.maturity <= timestamp;
                if (el("cfGardenHarvestButton")) el("cfGardenHarvestButton").hidden = !mature;
                if (el("cfGardenExitButton")) el("cfGardenExitButton").hidden = mature;
                setStatus("Viewing " + (position.version === "v1" ? "legacy V1" : "current V2") + " Garden. Harvest/exit targets this deployment only.");
            } else {
                _selectedGarden = null; _plantedAt = _maturityAt = _chainTimeOffset = 0; stopGrowthTicker();
                setText("cfGardenTitle", boot.v2Enabled ? "Plant a Garden" : "Your Garden");
                if (legacyUnreadable) setStatus("Legacy Garden could not be read. Planting is disabled to avoid creating a duplicate position.", true);
                else if (!boot.v2Enabled) setStatus("Your legacy Garden is clear. V2 planting and sponsorship are not available.");
                else if (_plantingPaused) setStatus("New Garden planting is temporarily paused by the contract owner. You can still support optional Garden bonuses.");
                else if (v1 && !v1.active && !v2.active) setStatus("Choose 1–" + _maxSeeds + " seeds. Each seed costs " + money(_seedPrice) + ". Your Garden transaction will check gas support before opening your wallet.");
                else setStatus("No active Garden found.");
            }
        } catch (error) {
            if (_seedPrice <= 0n) setText("cfGardenSeedUnitPrice", "Price unavailable");
            setStatus((error && error.message) || "Could not read Garden details. Check your connection and try again.", true);
        }
    }

    async function send(abi, methodArgs, to) {
        var loaded = await contracts();
        return loaded.provider.sendTx(to, abi, methodArgs, "0x0", { waitForReceipt: true });
    }

    function showWalletProcessing(title, message) {
        showGardenProcessing(title || "Waiting for wallet confirmation", message || "Confirm the transaction in your wallet. Keep this page open while it is processed on Celo…");
    }

    async function publishGardenSponsorAcknowledgement(txHash) {
        var response = await fetch("/community/api/garden/sponsor-acknowledgement", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ tx_hash: txHash })
        });
        var result = await response.json().catch(function () { return {}; });
        if (!response.ok || !result.success) {
            throw new Error(result.error || "The contribution was confirmed, but its acknowledgement is still processing. Retry this confirmation later; do not contribute again.");
        }
        return result;
    }

    function newGasRequestId() {
        var bytes = new Uint8Array(32);
        if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
            throw new Error("Secure random generation is unavailable. Reload the page and try again.");
        }
        window.crypto.getRandomValues(bytes);
        return "0x" + Array.from(bytes, function (value) { return value.toString(16).padStart(2, "0"); }).join("");
    }

    async function waitForSponsoredBalance(provider, wallet, result) {
        var requiredWei = BigInt(result.estimated_fee_wei || result.amount_wei || "0");
        if (requiredWei <= 0n || typeof provider.getBalance !== "function") {
            throw new Error("Could not verify the sponsored CELO balance. No Garden approval or purchase was submitted.");
        }
        var waitSeconds = Number(result.post_topup_wait_seconds);
        if (!Number.isFinite(waitSeconds) || waitSeconds < 30) waitSeconds = 30;
        var minimumWaitMs = Math.ceil(waitSeconds) * 1000;
        var timeoutMs = Math.max(120000, minimumWaitMs + 90000);
        var startedAt = Date.now();
        while (true) {
            var balanceReady = false;
            try { balanceReady = BigInt(await provider.getBalance(wallet)) >= requiredWei; } catch (_) {}
            var elapsed = Date.now() - startedAt;
            if (elapsed >= minimumWaitMs && balanceReady) return;
            if (elapsed >= timeoutMs) {
                throw new Error("The app gas top-up was confirmed, but the required CELO balance did not appear within 2 minutes. No Garden approval or purchase was submitted; check your wallet balance and retry.");
            }
            var remainingSeconds = Math.max(0, Math.ceil((minimumWaitMs - elapsed) / 1000));
            if (remainingSeconds > 0) {
                setStatus("Gas top-up confirmed. Waiting " + remainingSeconds + " more seconds and checking your CELO balance before opening your wallet…");
            } else {
                setStatus("The 30-second wait is complete. Waiting for enough CELO to appear in your wallet before opening your wallet…");
            }
            await new Promise(function (resolve) {
                window.setTimeout(resolve, Math.min(2000, timeoutMs - elapsed));
            });
        }
    }

    async function requestGardenGas(seeds, loaded, wallet) {
        var gasNotice = el("cfGardenTransactionNote");
        if (gasNotice) gasNotice.hidden = true;
        if (!_gasRequestId || _gasRequestSeeds !== seeds) {
            _gasRequestId = newGasRequestId();
            _gasRequestSeeds = seeds;
        }
        showGardenProcessing("Checking Garden gas support", "Checking your CELO balance and estimating the approval and seed-purchase gas before any wallet transaction is requested…");
        var response = await fetch("/api/community/garden/gas-sponsor", {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ seed_count: seeds, request_id: _gasRequestId })
        });
        var result = await response.json().catch(function () { return {}; });
        if (!response.ok || !result.success) throw new Error(result.error || "Garden gas support could not be prepared.");
        if (result.sponsored && result.sponsor_tx_hash) {
            if (gasNotice) gasNotice.hidden = false;
            showGardenProcessing("Confirming your gas top-up", "The app is sponsoring an estimated " + result.estimated_fee_celo + " CELO of gas. Waiting for the top-up to confirm before opening your wallet…");
            var provider = loaded.v2 && loaded.v2.runner && loaded.v2.runner.provider;
            if (!provider || typeof provider.waitForTransaction !== "function") {
                throw new Error("Could not monitor the app gas top-up. No Garden purchase was submitted; retry using the same request.");
            }
            var receipt = await provider.waitForTransaction(result.sponsor_tx_hash, 1, 120000);
            if (!receipt || Number(receipt.status) !== 1) {
                throw new Error("The app gas top-up did not confirm. No G$ purchase was made; retry later.");
            }
            showGardenProcessing("Gas top-up confirmed", "Checking that the sponsored CELO has arrived and waiting for the Garden safety delay before opening your wallet…");
            await waitForSponsoredBalance(provider, wallet, result);
            showGardenProcessing("Gas top-up successful", "The sponsored CELO is confirmed and available. Next, confirm the G$ approval or seed purchase in your wallet.");
            return result;
        }
        showGardenProcessing("Gas check complete", "Your wallet will pay the estimated Celo network fee (" + result.estimated_fee_celo + " CELO). Preparing your wallet confirmation…");
        return result;
    }

    async function plant() {
        if (_busy) return;
        if (!boot.v2Enabled || !_v2) { setStatus("Planting is available only on Garden V2.", true); return; }
        if (fvBlocked("plant seeds")) return;
        var input = el("cfGardenSeedCount");
        var seeds = Number(input && input.value);
        if (!Number.isInteger(seeds) || seeds < 1 || seeds > _maxSeeds) {
            setStatus("Choose a whole number of seeds from 1 to " + _maxSeeds + ".", true);
            if (input) input.focus();
            return;
        }
        var amount;
        var address;
        try {
            address = walletAddress();
            setBusy(true);
            showGardenProcessing("Checking your Garden", "Reading Garden availability, your G$ balance and the seed purchase requirements…");
            var loaded = await contracts();
            if (await loaded.v2.plantingPaused()) throw new Error("New Garden planting is temporarily paused by the contract owner.");
            var positions = await Promise.all([boot.legacyEnabled ? _v1.gardens(address) : Promise.resolve(null), _v2.gardens(address)]);
            if ((positions[0] && isActive(positions[0])) || isActive(positions[1])) throw new Error("An active V1 or V2 Garden already exists. Harvest or exit it before planting again.");
            amount = BigInt(seeds) * _seedPrice;
            var balance = await loaded.token.balanceOf(address);
            if (balance < amount) throw new Error("Insufficient G$ balance for " + seeds + (seeds === 1 ? " seed." : " seeds."));
            var allowance = await loaded.token.allowance(address, boot.contractAddress);
            var gasResult = await requestGardenGas(seeds, loaded, address);
            if (allowance < amount) {
                showWalletProcessing(gasResult && gasResult.sponsored ? "Gas top-up successful — approve G$" : "Approve your G$ purchase",
                    (gasResult && gasResult.sponsored ? "The sponsored CELO is ready. " : "Your wallet pays the Celo transaction fee. ") +
                    "Approve exactly " + money(amount) + " in your wallet. The Garden purchase will continue after this approval is confirmed.");
                await send("function approve(address spender,uint256 amount) returns (bool)", [boot.contractAddress, amount], boot.tokenAddress);
            }
            showWalletProcessing(gasResult && gasResult.sponsored ? "Gas top-up successful — confirm purchase" : "Confirm your Garden purchase",
                (gasResult && gasResult.sponsored ? "The sponsored CELO is ready. " : "Your wallet pays the Celo transaction fee. ") +
                "Confirm the seed purchase in your wallet. Your seeds will be planted after the transaction is confirmed on Celo…");
            await send("function buyAndPlant(uint256 seedCount)", [BigInt(seeds)], boot.contractAddress);
            _gasRequestId = null;
            _gasRequestSeeds = null;
            await refresh();
            setStatus("Garden purchase confirmed. Your 24-hour growth cycle has started.");
        } catch (error) {
            setStatus((error && error.message) || "Garden purchase failed or was cancelled.", true);
        } finally {
            hideGardenProcessing();
            setBusy(false);
        }
    }

    async function harvest() {
        if (_busy) return; if (fvBlocked("harvest")) return;
        try { if (!_selectedGarden || !_selectedGarden.active) throw new Error("Select an active Garden position first."); setBusy(true); showWalletProcessing("Confirm your Garden harvest", "Confirm the harvest from the selected " + _selectedGarden.version.toUpperCase() + " Garden in your wallet. Waiting for Celo confirmation…"); await send("function harvestAndWithdraw() returns (uint256 principalReturned,uint256 sponsorBonus)", [], _selectedGarden.address); await refresh(); setStatus("Harvest confirmed. Your Garden principal and any available bonus were withdrawn."); }
        catch (error) { setStatus(error.message || "Harvest failed or was cancelled.", true); } finally { hideGardenProcessing(); setBusy(false); }
    }
    async function exit() {
        if (_busy) return; if (fvBlocked("exit the Garden")) return;
        try { if (!_selectedGarden || !_selectedGarden.active) throw new Error("Select an active Garden position first."); setBusy(true); showWalletProcessing("Confirm your Garden exit", "Confirm the early exit from the selected " + _selectedGarden.version.toUpperCase() + " Garden in your wallet. Waiting for Celo confirmation…"); await send("function exitGarden() returns (uint256 principalReturned)", [], _selectedGarden.address); await refresh(); setStatus("Garden exit confirmed. Your principal was returned."); }
        catch (error) { setStatus(error.message || "Garden exit failed or was cancelled.", true); } finally { hideGardenProcessing(); setBusy(false); }
    }

    async function sponsor() {
        if (_busy) return;
        if (fvBlocked("sponsor the reward pool")) return;
        if (_pendingSponsorTxHash) {
            try {
                setBusy(true);
                showGardenProcessing("Retrying Garden acknowledgement", "Your contribution is already confirmed on Celo. Verifying it and restoring the pinned acknowledgement…");
                await publishGardenSponsorAcknowledgement(_pendingSponsorTxHash);
                _pendingSponsorTxHash = null;
                if (el("cfGardenSponsorAmount")) el("cfGardenSponsorAmount").value = "";
                var pendingAck = el("cfGardenSponsorAck");
                if (pendingAck) pendingAck.checked = false;
                updateSponsorButton();
                await refresh();
                setStatus("Sponsor contribution confirmed. Your pinned Garden acknowledgement is now visible in the Community Feed.");
            } catch (retryError) {
                setStatus("The contribution is confirmed, but the pinned acknowledgement is still processing. Tap contribute again to retry the same confirmation; do not contribute again.", true);
            } finally {
                hideGardenProcessing();
                setBusy(false);
            }
            return;
        }
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
            if (!boot.v2Enabled || !_v2) throw new Error("Sponsorship is available only on Garden V2.");
            var loaded = await contracts();
            var amount = _ethers.parseUnits(amountText, 18);
            if (!amount || amount <= 0n) throw new Error("Contribution must be greater than zero.");
            var wallet = walletAddress();
            setBusy(true);
            var balance = await loaded.token.balanceOf(wallet);
            if (balance < amount) throw new Error("Insufficient G$ balance for this contribution.");
            var allowance = await loaded.token.allowance(wallet, boot.contractAddress);
            if (allowance < amount) {
                showWalletProcessing("Approve your sponsor contribution", "Approve exactly " + amountText + " G$ in your wallet. The contribution will continue after this approval is confirmed.");
                await send("function approve(address spender,uint256 amount) returns (bool)", [boot.contractAddress, amount], boot.tokenAddress);
            }
            showWalletProcessing("Confirm sponsor contribution", "Confirm the irreversible " + amountText + " G$ contribution in your wallet. Waiting for Celo confirmation…");
            var sponsorTxHash = await send("function fundSponsorPool(uint256 amount)", [amount], boot.contractAddress);
            _pendingSponsorTxHash = sponsorTxHash;
            showGardenProcessing("Publishing your Garden acknowledgement", "Your contribution is confirmed on Celo. Verifying it and adding your pinned acknowledgement to the Community Feed…");
            try {
                await publishGardenSponsorAcknowledgement(sponsorTxHash);
                _pendingSponsorTxHash = null;
            } catch (ackError) {
                await refresh();
                setStatus("Sponsor contribution confirmed, but the pinned acknowledgement is still processing. Retry the same confirmation if needed; do not contribute again.", true);
                return;
            }
            if (el("cfGardenSponsorAmount")) el("cfGardenSponsorAmount").value = "";
            if (acknowledgement) acknowledgement.checked = false;
            updateSponsorButton();
            await refresh();
            setStatus("Sponsor contribution confirmed. Your pinned Garden acknowledgement is now visible in the Community Feed.");
        } catch (error) {
            setStatus((error && error.message) || "Sponsor contribution failed or was cancelled.", true);
        } finally {
            hideGardenProcessing();
            setBusy(false);
        }
    }

    function open(trigger) {
        if (!fvVerified()) return requireFaceVerification();
        if (window.GMCommunity && typeof window.GMCommunity.closeSheet === "function") {
            window.GMCommunity.closeSheet(false);
        }
        _returnFocus = trigger || document.getElementById("cfMenuGarden") || null;
        modal.hidden = false;
        modal.setAttribute("aria-hidden", "false");
        hideGardenProcessing();
        document.body.classList.add("cf-modal-open");
        var card = modal.querySelector(".cf-garden-card");
        if (card && card.focus) card.focus();
        setStatus("Loading your Celo Garden…");
        refresh();
        return true;
    }

    function close() {
        if (modal.hidden) return;
        var processing = el("cfGardenProcessing");
        if (processing && !processing.hidden) return;
        stopGrowthTicker();
        modal.hidden = true;
        modal.setAttribute("aria-hidden", "true");
        document.body.classList.remove("cf-modal-open");
        var trigger = _returnFocus;
        _returnFocus = null;
        if (trigger && trigger.focus) trigger.focus();
    }

    var seedInput = el("cfGardenSeedCount");
    if (seedInput) {
        seedInput.max = String(_maxSeeds);
        seedInput.addEventListener("input", updateTotal);
    }
    var sponsorAcknowledgement = el("cfGardenSponsorAck");
    if (sponsorAcknowledgement) sponsorAcknowledgement.addEventListener("change", updateSponsorButton);
    updateSponsorButton();
    var positionSelect = el("cfGardenPositionSelect");
    if (positionSelect) positionSelect.addEventListener("change", function () { _selectedVersion = positionSelect.value; refresh(); });
    document.addEventListener("keydown", function (event) {
        if (!modal.hidden && event.key === "Escape") close();
    });
    window.GMCommunityGarden = { open: open, close: close, refresh: refresh, plant: plant, harvest: harvest, exit: exit, sponsor: sponsor };
})();
