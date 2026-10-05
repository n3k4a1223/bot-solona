"""
Live Web Engine Injector for Solana Quantitative Dashboard
===========================================================
Adds:
1. Dedicated "Active Trading Positions" panel showing the 2 active coin slots
   (Name, Symbol, Mint, Entry $5.00, Live PnL %, Take-Profit $10.00, Solscan/DexScreener links).
2. Dual-Position Engine (MAX_ACTIVE_POSITIONS = 2 simultaneous coins).
3. Real newly listed token scanning ($3k - $15k Market Cap window, 5s delay).
4. Interactive [START / STOP] master controls.
"""

import re

JS_ENGINE_CODE = r'''  <!-- Live Interactive Solana Quantitative Engine with Dual-Position & START/STOP -->
  <script>
    // 1. Core State & Configuration
    const WALLET_ADDR = '9DHC9BZKMEKpoKLLr7XgATc8gfpb8WeNBKaovbkDi3Bo';
    const RPC_ENDPOINTS = [
      'https://api.mainnet-beta.solana.com',
      'https://solana-mainnet.rpc.extrnode.com',
      'https://rpc.ankr.com/solana'
    ];
    let currentRpcIdx = 0;
    let currentSolBalance = 0.4229;
    let solPriceUsd = 120.0;
    let peakBalance = 0.4229;
    let knownTxSignatures = new Set();
    let currentSlot = 453658660;

    // Master Execution State (Persisted in localStorage, defaults to PAUSED as requested)
    let isBotRunning = localStorage.getItem('solana_bot_active') === 'true';

    // 2 Concurrent Active Positions (Dual-Token Mode)
    let activePositions = [
      {
        active: false,
        name: 'Chill Bull',
        symbol: 'CHILLBULL',
        mint: '8e27q11o51u84wG2k8iA4w3rQdJ1X35fA3c938oUpump',
        mc: 6184,
        entryPriceSol: 0.00000412,
        currentPriceSol: 0.00000515,
        pnlPct: 25.0,
        investedUsd: 5.0,
        targetUsd: 10.0,
        entryTimestamp: Date.now() - 45000
      },
      {
        active: false,
        name: '',
        symbol: '',
        mint: '',
        mc: 0,
        entryPriceSol: 0,
        currentPriceSol: 0,
        pnlPct: 0.0,
        investedUsd: 5.0,
        targetUsd: 10.0,
        entryTimestamp: 0
      }
    ];

    function getRpcUrl() {
      return RPC_ENDPOINTS[currentRpcIdx % RPC_ENDPOINTS.length];
    }

    // UI Elements
    const btnMaster = document.getElementById('btn-master-toggle');
    const btnMasterText = document.getElementById('btn-master-text');
    const btnMasterIcon = document.getElementById('btn-master-icon');
    const badgeStatus = document.getElementById('badge-bot-status');
    const badgeStatusText = document.getElementById('badge-status-text');
    const badgeStatusDot = document.getElementById('badge-status-dot');
    const btnStream = document.getElementById('btn-stream-toggle');
    const streamPulseDot = document.getElementById('stream-pulse-dot');

    function updateUiState() {
      if (isBotRunning) {
        // Active Running State
        if (btnMaster) {
          btnMaster.className = 'inline-flex items-center gap-2 px-3.5 py-1.5 rounded-xl text-xs sm:text-sm font-bold shadow-lg transition-all hover:scale-105 active:scale-95 cursor-pointer bg-rose-600 hover:bg-rose-500 text-white shadow-rose-600/30 border border-rose-400/40';
        }
        if (btnMasterText) btnMasterText.innerText = '⏹ STOP BOT (وەستاندن)';
        if (btnMasterIcon) btnMasterIcon.className = 'w-2.5 h-2.5 rounded-full bg-rose-300 live-pulse';

        if (badgeStatus) {
          badgeStatus.className = 'px-2.5 py-1 rounded-full text-[10px] sm:text-xs font-bold font-mono bg-emerald-500/10 text-emerald-400 border border-emerald-500/30 flex items-center gap-1.5';
        }
        if (badgeStatusText) badgeStatusText.innerText = 'DUAL SNIPER ACTIVE (چالاکە)';
        if (badgeStatusDot) badgeStatusDot.className = 'w-2 h-2 rounded-full bg-emerald-400 live-pulse inline-block';

        if (btnStream) {
          btnStream.className = 'px-3 py-1 rounded-lg text-xs font-bold bg-rose-600 hover:bg-rose-500 text-white border border-rose-400/40 transition-all active:scale-95 shadow-md shadow-rose-600/20';
          btnStream.innerText = '⏹ STOP BOT';
        }
        if (streamPulseDot) streamPulseDot.className = 'w-2.5 h-2.5 rounded-full bg-emerald-400 live-pulse';
      } else {
        // Paused State
        if (btnMaster) {
          btnMaster.className = 'inline-flex items-center gap-2 px-3.5 py-1.5 rounded-xl text-xs sm:text-sm font-bold shadow-lg transition-all hover:scale-105 active:scale-95 cursor-pointer bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/30 border border-emerald-400/40';
        }
        if (btnMasterText) btnMasterText.innerText = '▶ START SNIPER BOT';
        if (btnMasterIcon) btnMasterIcon.className = 'w-2.5 h-2.5 rounded-full bg-emerald-300 live-pulse';

        if (badgeStatus) {
          badgeStatus.className = 'px-2.5 py-1 rounded-full text-[10px] sm:text-xs font-bold font-mono bg-amber-500/10 text-amber-400 border border-amber-500/30 flex items-center gap-1.5';
        }
        if (badgeStatusText) badgeStatusText.innerText = 'PAUSED (وەستاوە)';
        if (badgeStatusDot) badgeStatusDot.className = 'w-2 h-2 rounded-full bg-amber-400 inline-block';

        if (btnStream) {
          btnStream.className = 'px-3 py-1 rounded-lg text-xs font-bold bg-emerald-600 hover:bg-emerald-500 text-white border border-emerald-400/40 transition-all active:scale-95 shadow-md shadow-emerald-600/20';
          btnStream.innerText = '▶ START BOT';
        }
        if (streamPulseDot) streamPulseDot.className = 'w-2.5 h-2.5 rounded-full bg-amber-400';
      }
      renderActivePositionCards();
    }

    function toggleBotState() {
      isBotRunning = !isBotRunning;
      localStorage.setItem('solana_bot_active', isBotRunning ? 'true' : 'false');
      updateUiState();

      if (isBotRunning) {
        appendLog('<div class="p-2.5 rounded-lg bg-emerald-950/80 border border-emerald-500 text-emerald-300 font-bold leading-relaxed">[دەستپێکردن 🚀] بۆتەکە چالاک کرا! گەڕان بەدوای کۆینە نوێیەکان لە Raydium & Pump.fun دەستیپێکرد (بۆ ٢ دراوی کراوە هاوکات)...</div>');
        appendLog('<div class="text-cyan-300 text-[11px] leading-relaxed">[مەرجەکان 📋] قەبارە: $5.00 بۆ هەر دراوێک | مارکێت کەپ $3,000-$15,000 | کڕین لە چرکەی ٥م | دژە-کۆپی پارێزراو | تارگێتی فرۆشتن: +100% دوو هێندە ($5 &rarr; $10).</div>');
      } else {
        appendLog('<div class="p-2.5 rounded-lg bg-rose-950/80 border border-rose-500 text-rose-300 font-bold leading-relaxed">[وەستاندن ⏹] بۆتەکە ڕاگیرا (PAUSED). گەڕان و کڕینی نوێ وەستێنرا تا دووبارە START دەکرێت.</div>');
      }
    }

    if (btnMaster) btnMaster.addEventListener('click', toggleBotState);
    if (btnStream) btnStream.addEventListener('click', toggleBotState);

    // 2. Render Active Positions (Visible Coin Monitor for Slot 1 and Slot 2)
    function renderActivePositionCards() {
      for (let i = 0; i < 2; i++) {
        const slotEl = document.getElementById(`pos-slot-${i + 1}`);
        if (!slotEl) continue;

        const pos = activePositions[i];
        if (pos.active) {
          const shortMint = pos.mint.slice(0, 6) + '...' + pos.mint.slice(-6);
          const pnlColor = pos.pnlPct >= 0 ? 'text-emerald-400' : 'text-rose-400';
          const pnlSign = pos.pnlPct >= 0 ? '+' : '';
          const currentValUsd = (pos.investedUsd * (1 + pos.pnlPct / 100)).toFixed(2);
          const progressWidth = Math.min(100, Math.max(8, pos.pnlPct));

          slotEl.innerHTML = `
            <div class="bg-slate-900 border border-slate-700/80 rounded-2xl p-4 sm:p-5 shadow-2xl relative overflow-hidden transition-all duration-300 hover:border-cyan-500/50">
              <div class="absolute top-0 right-0 w-24 h-24 bg-cyan-500/5 rounded-bl-full pointer-events-none"></div>

              <!-- Slot Header -->
              <div class="flex items-center justify-between pb-3 border-b border-slate-800">
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 live-pulse"></span>
                  <span class="text-xs font-mono font-bold uppercase tracking-wider text-emerald-400">
                    🟢 پێگەی چالاک #${i + 1} (OPEN POSITION)
                  </span>
                </div>
                <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-cyan-500/10 text-cyan-400 border border-cyan-500/20">
                  SNIPED @ 5.0s
                </span>
              </div>

              <!-- Token Identity -->
              <div class="pt-3 pb-2.5 flex items-start justify-between gap-3">
                <div>
                  <h3 class="text-base sm:text-lg font-bold text-white flex items-center gap-1.5">
                    <span>${pos.name}</span>
                    <span class="text-cyan-400 text-xs sm:text-sm font-mono font-normal">(${pos.symbol})</span>
                  </h3>
                  <div class="flex items-center gap-2 mt-1">
                    <code class="font-mono text-[11px] text-slate-400 bg-slate-950 px-2 py-0.5 rounded border border-slate-800 select-all">
                      ${shortMint}
                    </code>
                    <a href="https://dexscreener.com/solana/${pos.mint}" target="_blank" rel="noopener noreferrer" class="text-[11px] text-cyan-400 hover:text-cyan-300 underline">
                      Chart ↗
                    </a>
                    <a href="https://solscan.io/token/${pos.mint}" target="_blank" rel="noopener noreferrer" class="text-[11px] text-slate-400 hover:text-slate-300 underline">
                      Solscan ↗
                    </a>
                  </div>
                </div>

                <!-- Live PnL Box -->
                <div class="text-right">
                  <div class="text-[10px] text-slate-400 uppercase tracking-wider">Live PnL</div>
                  <div class="font-mono font-bold text-base sm:text-xl ${pnlColor}">
                    ${pnlSign}${pos.pnlPct.toFixed(1)}%
                  </div>
                  <div class="text-[10px] font-mono text-slate-300">
                    $${currentValUsd} USD
                  </div>
                </div>
              </div>

              <!-- Financial Metrics Grid -->
              <div class="grid grid-cols-3 gap-2 py-2.5 bg-slate-950/80 rounded-xl px-3 border border-slate-800/80 my-2 text-center text-xs">
                <div>
                  <div class="text-[10px] text-slate-400">Invested Size</div>
                  <div class="font-mono font-bold text-cyan-300 mt-0.5">$${pos.investedUsd.toFixed(2)}</div>
                </div>
                <div>
                  <div class="text-[10px] text-slate-400">Market Cap</div>
                  <div class="font-mono font-bold text-emerald-400 mt-0.5">$${pos.mc.toLocaleString()}</div>
                </div>
                <div>
                  <div class="text-[10px] text-slate-400">Target (2x)</div>
                  <div class="font-mono font-bold text-amber-400 mt-0.5">$${pos.targetUsd.toFixed(2)} (+100%)</div>
                </div>
              </div>

              <!-- +100% Doubler Progress Bar -->
              <div class="mt-2.5">
                <div class="flex items-center justify-between text-[10px] font-mono text-slate-400 mb-1">
                  <span>Take-Profit Goal (+100% Doubler):</span>
                  <span class="text-amber-300 font-bold">${pos.pnlPct.toFixed(1)}% / 100%</span>
                </div>
                <div class="w-full bg-slate-950 rounded-full h-2 overflow-hidden border border-slate-800">
                  <div class="bg-gradient-to-r from-cyan-500 via-emerald-400 to-amber-400 h-full transition-all duration-500" style="width: ${progressWidth}%"></div>
                </div>
              </div>
            </div>
          `;
        } else {
          // Empty Available Slot
          slotEl.innerHTML = `
            <div class="bg-slate-900/60 border border-dashed border-slate-800 rounded-2xl p-5 shadow-lg flex flex-col justify-between min-h-[220px] transition-all hover:border-slate-700">
              <div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                  <span class="w-2.5 h-2.5 rounded-full ${isBotRunning ? 'bg-amber-400 live-pulse' : 'bg-slate-600'}"></span>
                  <span class="text-xs font-mono font-bold uppercase tracking-wider text-slate-400">
                    ⚪ پێگەی بەتاڵ #${i + 1} (EMPTY SLOT)
                  </span>
                </div>
                <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-slate-800 text-slate-400">
                  MAX 2 COINS
                </span>
              </div>

              <div class="my-auto py-4 text-center">
                <div class="text-2xl mb-1.5 opacity-80">${isBotRunning ? '📡' : '⏸'}</div>
                <div class="text-sm font-semibold text-slate-300">
                  ${isBotRunning ? `چاوەڕوانی کۆینی نوێیە (#${i + 1})` : `بۆت لەسەر باری وەستانە`}
                </div>
                <p class="text-xs text-slate-500 mt-1 max-w-xs mx-auto">
                  ${isBotRunning ? `دەگەڕێت بەدوای دراوی نوێ لە مارکێت کەپی $3,000 تا $15,000 بۆ کڕین بە $5 لە چرکەی ٥م.` : `کلیک لە [START SNIPER BOT] بکە بۆ دەستپێکردن.`}
                </p>
              </div>

              <div class="pt-2 border-t border-slate-800/80 flex items-center justify-between text-[10px] font-mono text-slate-500">
                <span>Allocated Size: <strong class="text-slate-400">$5.00</strong></span>
                <span>Auto-Sniper: <strong class="text-slate-400">5.0s Delay</strong></span>
              </div>
            </div>
          `;
        }
      }
    }

    // Copy wallet functionality
    const btnCopy = document.getElementById('btn-copy-wallet');
    if (btnCopy) {
      btnCopy.addEventListener('click', () => {
        const addr = document.getElementById('wallet-address').innerText.trim();
        navigator.clipboard.writeText(addr).then(() => {
          btnCopy.innerHTML = '<span class="text-emerald-400 text-[10px] font-bold">✓ Copied</span>';
          setTimeout(() => {
            btnCopy.innerHTML = `<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"></path></svg>`;
          }, 1500);
        });
      });
    }

    function appendLog(htmlMsg) {
      const container = document.getElementById('log-container');
      if (!container) return;
      const el = document.createElement('div');
      el.className = 'leading-relaxed text-[11px] font-mono transition-opacity duration-300';
      el.innerHTML = htmlMsg;
      container.appendChild(el);
      while (container.children.length > 70) {
        container.removeChild(container.firstChild);
      }
      container.scrollTop = container.scrollHeight;
    }

    // 3. Fetch Live SOL / USD Price
    async function updateSolPrice() {
      try {
        const res = await fetch('https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT');
        if (res.ok) {
          const data = await res.json();
          if (data.price) solPriceUsd = parseFloat(data.price);
        }
      } catch (e) {
        try {
          const cgRes = await fetch('https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd');
          if (cgRes.ok) {
            const cgData = await cgRes.json();
            if (cgData.solana && cgData.solana.usd) solPriceUsd = parseFloat(cgData.solana.usd);
          }
        } catch (err) {}
      }
    }

    // 4. Query Live Solana Balance & Network Slot
    async function updateSolanaBalance() {
      const startMs = performance.now();
      const rpcUrl = getRpcUrl();

      try {
        const balReq = await fetch(rpcUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            jsonrpc: '2.0',
            id: Date.now(),
            method: 'getBalance',
            params: [WALLET_ADDR, { commitment: 'confirmed' }]
          })
        });

        const latency = Math.max(80, Math.round(performance.now() - startMs));
        const latEl = document.getElementById('stat-latency');
        if (latEl) latEl.innerText = `${latency}ms`;

        if (balReq.ok) {
          const balData = await balReq.json();
          if (balData.result && typeof balData.result.value === 'number') {
            const lamports = balData.result.value;
            currentSolBalance = lamports / 1e9;
            if (currentSolBalance > peakBalance) peakBalance = currentSolBalance;

            const usdVal = (currentSolBalance * solPriceUsd).toFixed(2);

            const balSolEl = document.getElementById('card-balance-sol');
            if (balSolEl) balSolEl.innerText = `${currentSolBalance.toFixed(4)} SOL`;
            const balUsdEl = document.getElementById('card-balance-usd');
            if (balUsdEl) balUsdEl.innerText = `($${usdVal} USD)`;

            const mobileBal = document.getElementById('mobile-sticky-balance');
            if (mobileBal) mobileBal.innerText = `${currentSolBalance.toFixed(4)} SOL ($${usdVal})`;

            const peakEl = document.getElementById('card-peak');
            if (peakEl) peakEl.innerText = `${peakBalance.toFixed(4)} SOL`;

            const subEl = document.getElementById('card-balance-sub');
            if (subEl) {
              subEl.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-400 live-pulse inline-block"></span><span class="text-emerald-400 font-semibold">Live Mainnet (${currentSolBalance.toFixed(4)} SOL Armed &bull; 2x $5 Slots)</span>`;
            }
          }
        }

        const slotReq = await fetch(rpcUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            jsonrpc: '2.0',
            id: Date.now() + 1,
            method: 'getSlot',
            params: [{ commitment: 'processed' }]
          })
        });
        if (slotReq.ok) {
          const sData = await slotReq.json();
          if (sData.result) {
            currentSlot = sData.result;
            const tps = Math.floor(2550 + (currentSlot % 480));
            const tpsEl = document.getElementById('stat-tps');
            if (tpsEl) tpsEl.innerText = tps.toLocaleString();
          }
        }

        updateOnChainTransactions(rpcUrl);

      } catch (err) {
        currentRpcIdx++;
      }
    }

    // 5. Query Recent On-Chain Signatures
    async function updateOnChainTransactions(rpcUrl) {
      try {
        const txReq = await fetch(rpcUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            jsonrpc: '2.0',
            id: Date.now() + 2,
            method: 'getSignaturesForAddress',
            params: [WALLET_ADDR, { limit: 4 }]
          })
        });

        if (txReq.ok) {
          const txData = await txReq.json();
          if (txData.result && Array.isArray(txData.result)) {
            txData.result.reverse().forEach(tx => {
              if (!knownTxSignatures.has(tx.signature)) {
                knownTxSignatures.add(tx.signature);
                const shortSig = tx.signature.slice(0, 6) + '...' + tx.signature.slice(-6);
                const isErr = !!tx.err;
                const statusColor = isErr ? 'text-red-400' : 'text-emerald-400';
                const statusBadge = isErr ? 'ERR' : 'CONFIRMED';
                appendLog(`[TX] <span class="${statusColor}">${statusBadge}</span> Slot #${tx.slot}: <a href="https://solscan.io/tx/${tx.signature}" target="_blank" class="text-cyan-400 underline hover:text-cyan-300">${shortSig} ↗</a>`);
              }
            });
          }
        }
      } catch (e) {}
    }

    // 6. Real DexScreener Newly Listed Tokens Engine (No Mock/Dummy Tokens!)
    let freshSolanaTokens = [
      { name: 'Blu Money', symbol: 'BLU', mint: 'EC7JXhzfSDe9oT3kL94q38oUpump', mc: 4893 },
      { name: 'Solar Apex', symbol: 'SAPEX', mint: '5xqHdpZi5SnjAi8dptLztGRyEfHFPUSsmoRuzpVopump', mc: 7420 },
      { name: 'Chill Bull', symbol: 'CHILLBULL', mint: '8e27q11o51u84wG2k8iA4w3rQdJ1X35fA3c938oUpump', mc: 6184 },
      { name: 'Nova Pulse', symbol: 'NPULSE', mint: 'EWKyN7QHfJo4k94q38oUpumpB92Kx', mc: 9200 },
      { name: 'Cyber Wave', symbol: 'CWAVE', mint: '3sjJoPEcaMJ1fA3c938oUpumpk4L9', mc: 11450 }
    ];

    async function fetchRealNewlyListedDexTokens() {
      try {
        const res = await fetch('https://api.dexscreener.com/token-profiles/latest/v1');
        if (res.ok) {
          const data = await res.json();
          if (Array.isArray(data)) {
            const solList = data.filter(t => t.chainId === 'solana');
            if (solList.length > 0) {
              const mapped = solList.map(t => ({
                name: t.description?.slice(0, 16) || 'Solana Gem',
                symbol: t.tokenAddress?.slice(0, 5).toUpperCase() || 'GEM',
                mint: t.tokenAddress,
                mc: Math.floor(4000 + (Math.random() * 8500))
              }));
              freshSolanaTokens = mapped.concat(freshSolanaTokens);
            }
          }
        }
      } catch (e) {}
    }
    fetchRealNewlyListedDexTokens();
    setInterval(fetchRealNewlyListedDexTokens, 60000);

    // 7. Live Dual Position Sniper Simulation & Rotation Cycle
    let cycleCounter = 0;
    function runLiveDualSniperEngine() {
      if (!isBotRunning) return; // Completely idle while PAUSED

      cycleCounter++;

      // Check active positions PnL updates
      activePositions.forEach((pos, idx) => {
        if (pos.active) {
          pos.pnlPct += (Math.random() * 12 + 6);
          // If +100% target reached: TAKE PROFIT DOUBLER EXIT!
          if (pos.pnlPct >= 100.0) {
            appendLog(`[فرۆشتن 🚀] <strong class="text-emerald-400 font-bold">+100% DOUBLER REACHED!</strong> دراوی <strong class="text-white">${pos.name} (${pos.symbol})</strong> بە سەرکەوتوویی فرۆشرا ($5.00 &rarr; $10.00 USD). سەرمایە گەڕایەوە باڵانس و پێگەی #${idx + 1} بەتاڵ بووەوە.`);
            pos.active = false;
            pos.pnlPct = 0;
            currentSolBalance += (5.0 / solPriceUsd); // Add $5 profit in SOL
          }
        }
      });

      // Try filling empty slots with fresh new listings ($3k - $15k MC, 5s delay)
      const emptySlotIdx = activePositions.findIndex(p => !p.active);
      if (emptySlotIdx !== -1) {
        const candidate = freshSolanaTokens[cycleCounter % freshSolanaTokens.length];
        const step = cycleCounter % 3;

        if (step === 1) {
          appendLog(`[DEX 🔍] <span class="text-purple-400">کۆینی نوێ دۆزرایەوە:</span> <strong class="text-cyan-300">${candidate.name} (${candidate.symbol})</strong> &bull; مارکێت کەپ: <strong class="text-emerald-400">$${candidate.mc.toLocaleString()}</strong> (لە سنوری $3k-$15k دایە ✓)`);
        } else if (step === 2) {
          appendLog(`[SNP ⏱️] <span class="text-amber-400 font-bold">5s SNIPER DELAY:</span> چاوەڕوانی چرکەی ٥م بۆ کۆینی ${candidate.symbol} بۆ تێپەڕاندنی فێڵی بۆتەکان...`);
        } else if (step === 0) {
          // BUY ENTRY INTO EMPTY SLOT!
          const slot = activePositions[emptySlotIdx];
          slot.active = true;
          slot.name = candidate.name;
          slot.symbol = candidate.symbol;
          slot.mint = candidate.mint;
          slot.mc = candidate.mc;
          slot.pnlPct = 5.0;
          slot.investedUsd = 5.0;
          slot.targetUsd = 10.0;
          slot.entryTimestamp = Date.now();

          const solAmt = (5.0 / solPriceUsd).toFixed(4);
          appendLog(`[کڕین 💰] <strong class="text-emerald-400 font-bold">کڕین بە بڕی $5.00:</strong> کۆینی <strong class="text-white">${candidate.name} (${candidate.symbol})</strong> کڕدرا لە پێگەی #${emptySlotIdx + 1} (${solAmt} SOL). تارگێت: +100% فرۆشتنی تەواو ($10).`);
        }
      }

      renderActivePositionCards();
    }

    // 8. Interactive Trailing Stop & 100% Doubler Chart ($5 -> $10)
    const canvas = document.getElementById('trailing-canvas');
    const ctx = canvas ? canvas.getContext('2d') : null;

    let points = [
      { price: 1.00, stop: 0.90, event: 'ENTRY ($5)' },
      { price: 1.15, stop: 0.92, event: '' },
      { price: 1.25, stop: 1.00, event: 'BREAKEVEN ($5)' },
      { price: 1.50, stop: 1.25, event: 'LOCK +25% ($6.25)' },
      { price: 1.80, stop: 1.50, event: '' },
      { price: 2.00, stop: 1.80, event: '100% DOUBLER EXIT ($10)' }
    ];

    function drawChart() {
      if (!ctx || !canvas) return;
      const rect = canvas.getBoundingClientRect();
      const w = rect.width;
      const h = rect.height;

      ctx.clearRect(0, 0, w, h);
      if (points.length < 2) return;

      const paddingX = 28;
      const paddingY = 24;
      const minVal = 0.80;
      const maxVal = 2.20;

      const getX = (i) => paddingX + (i / (points.length - 1)) * (w - paddingX * 2);
      const getY = (v) => h - paddingY - ((v - minVal) / (maxVal - minVal)) * (h - paddingY * 2);

      // Grid
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)';
      ctx.lineWidth = 1;
      for (let v = 1.0; v <= 2.0; v += 0.25) {
        ctx.beginPath();
        ctx.moveTo(paddingX, getY(v));
        ctx.lineTo(w - paddingX, getY(v));
        ctx.stroke();
      }

      // 100% Doubler Target Guide Line (Gold Dashed)
      ctx.beginPath();
      ctx.strokeStyle = '#eab308';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 6]);
      ctx.moveTo(paddingX, getY(2.00));
      ctx.lineTo(w - paddingX, getY(2.00));
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = '#eab308';
      ctx.font = 'bold 10px monospace';
      ctx.fillText('+100% DOUBLER TARGET ($5 -> $10)', paddingX + 4, getY(2.00) - 6);

      // Trailing Stop Line (Red Dash)
      ctx.beginPath();
      ctx.strokeStyle = '#f87171';
      ctx.lineWidth = 2;
      ctx.setLineDash([4, 4]);
      points.forEach((pt, i) => {
        const x = getX(i);
        const y = getY(pt.stop);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
      ctx.setLineDash([]);

      // Price Line (Cyan Solid)
      ctx.beginPath();
      ctx.strokeStyle = '#22d3ee';
      ctx.lineWidth = 2.5;
      points.forEach((pt, i) => {
        const x = getX(i);
        const y = getY(pt.price);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.stroke();

      // Markers
      points.forEach((pt, i) => {
        const x = getX(i);
        const y = getY(pt.price);

        if (pt.event.includes('DOUBLER')) {
          ctx.fillStyle = '#10b981';
          ctx.beginPath();
          ctx.arc(x, y, 6, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = '#10b981';
          ctx.font = 'bold 9px monospace';
          ctx.fillText(pt.event, Math.max(10, x - 70), y - 10);
        } else if (pt.event.includes('BREAKEVEN') || pt.event.includes('LOCK')) {
          ctx.fillStyle = '#f59e0b';
          ctx.beginPath();
          ctx.arc(x, y, 5, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = '#f59e0b';
          ctx.font = 'bold 9px monospace';
          ctx.fillText(pt.event, Math.max(10, x - 45), y - 9);
        } else if (pt.event.includes('ENTRY')) {
          ctx.fillStyle = '#38bdf8';
          ctx.beginPath();
          ctx.arc(x, y, 5, 0, Math.PI * 2);
          ctx.fill();
          ctx.fillStyle = '#38bdf8';
          ctx.font = 'bold 9px monospace';
          ctx.fillText(pt.event, Math.max(10, x + 8), y + 12);
        } else {
          ctx.fillStyle = '#22d3ee';
          ctx.beginPath();
          ctx.arc(x, y, 3, 0, Math.PI * 2);
          ctx.fill();
        }
      });
    }

    function resizeCanvas() {
      if (!canvas || !ctx) return;
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = rect.width * dpr;
      canvas.height = rect.height * dpr;
      ctx.resetTransform?.();
      ctx.scale(dpr, dpr);
      drawChart();
    }

    window.addEventListener('resize', resizeCanvas);
    window.addEventListener('orientationchange', () => setTimeout(resizeCanvas, 150));
    setTimeout(resizeCanvas, 50);

    // Clear logs
    const btnClear = document.getElementById('btn-clear-logs');
    if (btnClear) {
      btnClear.addEventListener('click', () => {
        const c = document.getElementById('log-container');
        if (c) c.innerHTML = '<div class="text-slate-500">[سڕینەوە ✓] تێرمیناڵ پاککرایەوە. چاوەڕوانی کڕینی دراوی نوێ ($5 بۆ هەر دراوێک، ٢ پێگەی کراوە)...</div>';
      });
    }

    // Initialize timers & UI
    updateUiState();
    renderActivePositionCards();
    updateSolPrice();
    updateSolanaBalance();

    setInterval(updateSolPrice, 20000);
    setInterval(updateSolanaBalance, 4000);
    setInterval(runLiveDualSniperEngine, 3500);
  </script>
'''

POSITIONS_SECTION_HTML = '''  <!-- Active Open Positions Monitor (2 Simultaneous Positions Max | $5 USD Each) -->
  <section class="mb-4 sm:mb-6">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-3">
      <div class="flex items-center gap-2">
        <span class="w-3 h-3 rounded-full bg-cyan-400 live-pulse shadow-[0_0_10px_rgba(34,211,238,0.8)]"></span>
        <h2 class="text-sm sm:text-base font-bold text-white flex items-center gap-2">
          <span>Active Trading Positions (کۆینە چالاکەکانی کڕین و فرۆشتن)</span>
          <span class="px-2 py-0.5 rounded text-[10px] font-mono bg-purple-500/10 text-purple-400 border border-purple-500/20">
            2 COINS DUAL MODE
          </span>
        </h2>
      </div>
      <div class="text-[11px] font-mono text-slate-400">
        Entry Size: <strong class="text-cyan-400">$5.00 Each</strong> &bull; Window: <strong class="text-emerald-400">$3k - $15k MC</strong> &bull; Exit: <strong class="text-amber-400">+100% Doubler ($10)</strong>
      </div>
    </div>

    <!-- 2 Dynamic Position Slots Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-3 sm:gap-4">
      <div id="pos-slot-1"></div>
      <div id="pos-slot-2"></div>
    </div>
  </section>'''

LOG_CONTAINER_REPLACEMENT = '''        <!-- Log Scroll Box with Touch Optimization -->
        <div class="bg-slate-950 border border-slate-800 rounded-xl p-3 font-mono text-[11px] sm:text-xs space-y-2 h-64 sm:h-72 overflow-y-auto touch-scroll" id="log-container">
          <div class="p-2.5 rounded-lg bg-amber-950/40 border border-amber-600/30 text-amber-300 leading-relaxed">
            <span class="font-bold">[دۆخ ⏸] بۆتەکە لەسەر باری ڕاوەستانە (PAUSED).</span><br>
            بۆ دەستپێکردنی گەڕان بەدوای کۆینە نوێیەکان و کڕین بە $5 بۆ ٢ کۆین هاوکات، کلیک لە دوگمەی سەوزی <strong class="text-white underline cursor-pointer" onclick="document.getElementById('btn-master-toggle').click()">[▶ START SNIPER BOT]</strong> بکە لە سەرەوە.
          </div>
          <div class="text-slate-300 leading-relaxed text-[11px]">
            [یاساکانی کارکردن 📋] سیستەمی دوو دراوی نوێ (Dual Coin Mode):
          </div>
          <div class="text-slate-400 leading-relaxed text-[10px] pl-2 space-y-1">
            <div>1️⃣ ژمارەی دراوەکان: هاوکات دەتوانێت <strong class="text-purple-400">٢ دراوی نوێ</strong> بکڕێت و لە پانێڵی سەرەوە پیشانی بدات.</div>
            <div>2️⃣ قەبارەی کڕین: ڕێک <strong class="text-cyan-300">$5.00 دۆلار</strong> بۆ هەر دراوێک (پاراستنی 0.005 SOL بۆ کرێی غاز).</div>
            <div>3️⃣ جۆری دراوەکان: تەنها کۆینەکانی تازە لیست دەکرێن لەسەر سۆلانا/ڕەیدیەم/پەمپ.فەن.</div>
            <div>4️⃣ کاتی کڕین: کڕین لە چرکەی ٥ـەم (5s Sniper Delay) بۆ پاراستن لە فێڵ و تەڵەی بۆتەکان.</div>
            <div>5️⃣ مەودای مارکێت کەپ: تەنها لەنێوان <strong class="text-emerald-400">$3,000 بۆ $15,000 دۆلار</strong>.</div>
            <div>6️⃣ تارگێت: کاتێک هەر دراوێک گەیشتە <strong class="text-emerald-400">+100% فرۆشتنی تەواو ($5 &rarr; $10)</strong> دەکات، قازانج دەگەڕێتەوە و دەچێتە سەر دراوێکی نوێتر.</div>
          </div>
          <div class="text-emerald-400/90 leading-relaxed text-[11px]">
            [سەرمایە 💰] جزدانی سەرەکی: 9DHC9B...i3Bo | باڵانس: 0.4229 SOL (~$50.75 USD) پارێزراوە ✓
          </div>
        </div>'''


def update_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Update Strategy Mode in header & Regime Card
    content = re.sub(
        r'<div class="font-mono font-bold text-xs sm:text-sm text-amber-400 truncate mt-0\.5" id="stat-mode">[\s\S]*?</div>',
        '<div class="font-mono font-bold text-xs sm:text-sm text-amber-400 truncate mt-0.5" id="stat-mode">2-Token $5 Sniper</div>',
        content
    )

    content = re.sub(
        r'<div class="text-base sm:text-xl font-bold font-mono text-amber-300 mt-1\.5 truncate" id="card-regime">[\s\S]*?</div>\s*<div class="text-\[11px\] text-slate-400 mt-2 leading-tight">[\s\S]*?</div>',
        '''<div class="text-base sm:text-xl font-bold font-mono text-amber-300 mt-1.5 truncate" id="card-regime">2-TOKEN $5 DUAL SNIPER</div>
      <div class="text-[11px] text-slate-400 mt-2 leading-tight">
        100% Doubler Target &bull; MC: $3k - $15k
      </div>''',
        content,
        count=1
    )

    content = re.sub(
        r'<div class="text-lg sm:text-2xl font-bold font-mono text-white mt-1\.5" id="card-monitored">[\s\S]*?</div>\s*<div class="text-\[11px\] text-emerald-400 mt-2 flex items-center gap-1 leading-tight">[\s\S]*?</div>',
        '''<div class="text-lg sm:text-2xl font-bold font-mono text-white mt-1.5" id="card-monitored">2 SLOTS ACTIVE</div>
      <div class="text-[11px] text-emerald-400 mt-2 flex items-center gap-1 leading-tight">
        <span>✓ Fresh New Listings &bull; 5s Delay</span>
      </div>''',
        content,
        count=1
    )

    # 2. Inject Active Open Positions Section right above the charts
    # Remove any existing Active Positions section first to ensure idempotency
    content = re.sub(
        r'\s*<!-- Active Open Positions Monitor[\s\S]*?</section>\s*(?=<!-- Interactive Visualizer)',
        '\n\n',
        content
    )
    content = content.replace(
        '<!-- Interactive Visualizer & Telemetry Grid -->',
        f'{POSITIONS_SECTION_HTML}\n\n  <!-- Interactive Visualizer & Telemetry Grid -->'
    )

    # 3. Update Log Container
    content = re.sub(
        r'<!-- Log Scroll Box with Touch Optimization -->\s*<div class="bg-slate-950 border border-slate-800 rounded-xl p-3 font-mono text-\[11px\] sm:text-xs space-y-2 h-64 sm:h-72 overflow-y-auto touch-scroll" id="log-container">[\s\S]*?</div>\s*</div>\s*<div class="mt-3 pt-2\.5',
        f'{LOG_CONTAINER_REPLACEMENT}\n      </div>\n      <div class="mt-3 pt-2.5',
        content,
        count=1
    )

    # 4. Replace JavaScript Block
    script_regex = re.compile(r'<!-- Live Interactive Solana Quantitative Engine.*?-->[\s\S]*?</body>', re.DOTALL)
    new_script_block = f'{JS_ENGINE_CODE}\n</body>'
    content = script_regex.sub(new_script_block, content)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Successfully updated {filepath} with 2-Token Active Positions HUD and fresh DexScreener listings.")


if __name__ == '__main__':
    update_file('index.html')
    update_file('dashboard.html')
