"""
Token Metadata, Website Verification & Anti-Clone Auditor
==========================================================
Enforces:
1. Mandatory token website verification with strict name/domain matching.
2. Anti-impersonation and clone blacklist for major cryptocurrencies & equities
   (Bitcoin, Ethereum, Solana, Ripple, Dogecoin, Apple, Tesla, Nvidia, etc.).
3. Market Cap bounds validation ($3,000 - $15,000 USD).
"""

from __future__ import annotations

import re
from typing import Optional, Tuple
from urllib.parse import urlparse
import aiohttp

from core.logger import get_logger
from core.rpc_balancer import MultiRPCBalancer
from core.types import TokenMetadataAudit

# Major cryptocurrencies, founder names, and global equities to forbid clones/derivatives of
MAJOR_ASSETS_BLACKLIST = [
    # Cryptocurrencies & Canonical Tickers
    "bitcoin", "btc", "satoshi",
    "ethereum", "eth", "ether", "vitalik",
    "solana", "sol", "anatoly",
    "ripple", "xrp",
    "binance", "bnb", "cz",
    "cardano", "ada", "hoskinson",
    "dogecoin", "doge", "shiba", "shib",
    "tether", "usdt", "usdc", "circle",
    "avalanche", "avax",
    "polkadot", "dot",
    "chainlink", "link",
    "polygon", "matic",
    "near", "sui", "aptos",

    # Major Tech Equities & Titans
    "apple", "aapl",
    "tesla", "tsla", "elon", "musk",
    "nvidia", "nvda",
    "google", "googl", "alphabet",
    "microsoft", "msft",
    "amazon", "amzn",
    "meta", "facebook",
    "microstrategy", "mstr", "saylor",
]

# Generic social media & redirect sites that do NOT count as dedicated token websites
GENERIC_SOCIAL_DOMAINS = {
    "t.me", "telegram.org", "telegram.me",
    "twitter.com", "x.com",
    "linktr.ee", "linktree.com", "campsite.bio",
    "google.com", "youtube.com", "medium.com", "discord.com", "discord.gg",
    "pump.fun", "dexscreener.com", "birdeye.so", "solscan.io"
}


class TokenMetadataAuditor:
    """
    Validates token metadata, ensures authentic website ownership matching token identity,
    and blocks all impersonators of major coins and equities.
    """

    def __init__(
        self,
        rpc_balancer: MultiRPCBalancer,
        min_market_cap_usd: float = 3000.0,
        max_market_cap_usd: float = 15000.0,
        sol_price_usd: float = 120.0,
    ):
        self.rpc = rpc_balancer
        self.min_market_cap_usd = min_market_cap_usd
        self.max_market_cap_usd = max_market_cap_usd
        self.sol_price_usd = sol_price_usd
        self.logger = get_logger()

    @staticmethod
    def detect_major_clone(token_name: str, token_symbol: str) -> Tuple[bool, Optional[str]]:
        """
        Detects if a token is an impersonator, derivative, or clone of a major coin/stock.
        e.g. 'BabyBitcoin', 'Solana2.0', 'Wrapped ETH', 'TeslaBot', 'SHIB', etc.
        """
        name_clean = token_name.lower().strip()
        sym_clean = token_symbol.lower().strip()

        # 1. Exact match on high-profile symbols
        high_profile_symbols = {
            "btc", "wbtc", "eth", "weth", "sol", "wsol", "xrp", "bnb",
            "ada", "doge", "shib", "usdt", "usdc", "aapl", "tsla", "nvda", "mstr"
        }
        if sym_clean in high_profile_symbols:
            return True, sym_clean

        # 2. Check if major asset keyword is present in token name or symbol
        for asset in MAJOR_ASSETS_BLACKLIST:
            # For words >= 4 chars (bitcoin, solana, ethereum, tesla, etc.)
            if len(asset) >= 4 and asset in name_clean:
                return True, asset

            # For short symbols or exact words (btc, eth, sol, ada, etc.)
            if re.search(rf"\b{asset}\b", name_clean) or re.search(rf"\b{asset}\b", sym_clean):
                return True, asset

        return False, None

    @staticmethod
    def verify_website_match(token_name: str, token_symbol: str, website_url: Optional[str]) -> Tuple[bool, bool, Optional[str]]:
        """
        Verifies that:
        1. Token has a valid website URL (not empty, not social media link).
        2. Website domain matches or contains token name or symbol.
        Returns (has_website, matches_name, failure_reason).
        """
        if not website_url or not isinstance(website_url, str):
            return False, False, "No website provided. Mandatory dedicated website required."

        website_url = website_url.strip()
        if not website_url.startswith(("http://", "https://")):
            website_url = f"https://{website_url}"

        try:
            parsed = urlparse(website_url)
            domain = parsed.netloc.lower()
            if not domain:
                return False, False, f"Invalid website URL format: '{website_url}'"

            # Check if domain is a generic social or redirect service
            domain_no_www = re.sub(r"^(www\.)", "", domain)
            for generic in GENERIC_SOCIAL_DOMAINS:
                if domain_no_www == generic or domain_no_www.endswith(f".{generic}"):
                    return True, False, f"Website '{domain}' is a social media or generic platform, not a dedicated website."

            # Extract clean domain core (remove TLD and non-alphanumeric chars)
            domain_clean = re.sub(
                r"\.(com|io|xyz|org|net|fun|app|co|me|site|online|tech|vip|cc|biz|info|live)$",
                "",
                domain_no_www
            )
            domain_clean = re.sub(r"[^a-z0-9]", "", domain_clean)

            # Clean token name and symbol
            token_clean = re.sub(r"[^a-z0-9]", "", token_name.lower())
            symbol_clean = re.sub(r"[^a-z0-9]", "", token_symbol.lower())

            if not domain_clean:
                return True, False, f"Could not extract valid domain name from '{website_url}'."

            # Matching conditions:
            # 1. Clean token name is substring of domain
            # 2. Domain is substring of clean token name
            # 3. Clean symbol (>= 3 chars) is in domain
            is_match = (
                (token_clean and token_clean in domain_clean) or
                (domain_clean and domain_clean in token_clean) or
                (len(symbol_clean) >= 3 and symbol_clean in domain_clean)
            )

            if not is_match:
                return True, False, (
                    f"Website domain '{domain_no_www}' does not match token name '{token_name}' "
                    f"or symbol '{token_symbol}'."
                )

            return True, True, None

        except Exception as e:
            return False, False, f"Error parsing website URL: {e}"

    async def audit_token(
        self,
        token_mint: str,
        pool_address: str,
        sol_reserves: float,
        token_reserves: float,
        sol_price_usd: Optional[float] = None,
    ) -> TokenMetadataAudit:
        """
        Conducts end-to-end metadata audit:
        - Fetches token info from DexScreener API / on-chain
        - Rejects major coin/stock clones
        - Rejects missing website or mismatched website name
        - Validates market cap in [$3,000, $15,000] USD
        """
        price_usd = sol_price_usd or self.sol_price_usd
        token_name = "Unknown"
        token_symbol = "UNK"
        website_url: Optional[str] = None
        market_cap_usd = 0.0

        # Compute on-chain baseline Market Cap (FDV)
        if sol_reserves > 0 and token_reserves > 0:
            price_sol = sol_reserves / token_reserves
            # Base FDV from 1 Billion standard supply if RPC unavailable, or from reserves
            estimated_fdv_sol = 2.0 * sol_reserves
            market_cap_usd = estimated_fdv_sol * price_usd

        # 1. Fetch metadata from DexScreener API
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4.0)) as session:
                url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        pairs = data.get("pairs") or []
                        if pairs:
                            primary_pair = pairs[0]
                            base_tok = primary_pair.get("baseToken", {})
                            token_name = base_tok.get("name", token_name)
                            token_symbol = base_tok.get("symbol", token_symbol)

                            # Extract market cap
                            dex_mc = primary_pair.get("marketCap") or primary_pair.get("fdv")
                            if dex_mc and float(dex_mc) > 0:
                                market_cap_usd = float(dex_mc)

                            # Extract website
                            info = primary_pair.get("info", {})
                            websites = info.get("websites") or []
                            if websites and isinstance(websites, list):
                                website_url = websites[0].get("url")
        except Exception as e:
            self.logger.log_debug(f"DexScreener metadata query error for {token_mint[:8]}: {e}")

        # 2. Check for Major Coin / Stock Clones (Requirement 2)
        is_clone, matched_asset = self.detect_major_clone(token_name, token_symbol)
        if is_clone:
            fail_reason = (
                f"REJECTED: Impersonator/clone of major asset detected "
                f"('{token_name}' / '{token_symbol}' matches '{matched_asset}')."
            )
            return TokenMetadataAudit(
                token_mint=token_mint,
                token_name=token_name,
                token_symbol=token_symbol,
                website_url=website_url,
                has_website=bool(website_url),
                website_matches_name=False,
                is_major_clone=True,
                matched_major_asset=matched_asset,
                market_cap_usd=market_cap_usd,
                passed=False,
                failure_reason=fail_reason,
            )

        # 3. Check Market Cap USD Range [$3,000, $15,000] (Requirement 3)
        if market_cap_usd < self.min_market_cap_usd:
            fail_reason = (
                f"REJECTED: Market Cap ${market_cap_usd:,.2f} is below minimum threshold "
                f"(${self.min_market_cap_usd:,.2f} USD). Too early / low liquidity."
            )
            return TokenMetadataAudit(
                token_mint=token_mint,
                token_name=token_name,
                token_symbol=token_symbol,
                website_url=website_url,
                has_website=bool(website_url),
                website_matches_name=False,
                is_major_clone=False,
                matched_major_asset=None,
                market_cap_usd=market_cap_usd,
                passed=False,
                failure_reason=fail_reason,
            )

        if market_cap_usd > self.max_market_cap_usd:
            fail_reason = (
                f"REJECTED: Market Cap ${market_cap_usd:,.2f} exceeds maximum threshold "
                f"(${self.max_market_cap_usd:,.2f} USD). Outside gem entry window."
            )
            return TokenMetadataAudit(
                token_mint=token_mint,
                token_name=token_name,
                token_symbol=token_symbol,
                website_url=website_url,
                has_website=bool(website_url),
                website_matches_name=False,
                is_major_clone=False,
                matched_major_asset=None,
                market_cap_usd=market_cap_usd,
                passed=False,
                failure_reason=fail_reason,
            )

        # 4. Check Website Requirement and Matching (Requirement 1)
        has_website, matches_name, web_fail = self.verify_website_match(token_name, token_symbol, website_url)
        if not has_website or not matches_name:
            fail_reason = f"REJECTED: {web_fail}"
            return TokenMetadataAudit(
                token_mint=token_mint,
                token_name=token_name,
                token_symbol=token_symbol,
                website_url=website_url,
                has_website=has_website,
                website_matches_name=matches_name,
                is_major_clone=False,
                matched_major_asset=None,
                market_cap_usd=market_cap_usd,
                passed=False,
                failure_reason=fail_reason,
            )

        # All 3 requirements passed!
        return TokenMetadataAudit(
            token_mint=token_mint,
            token_name=token_name,
            token_symbol=token_symbol,
            website_url=website_url,
            has_website=True,
            website_matches_name=True,
            is_major_clone=False,
            matched_major_asset=None,
            market_cap_usd=market_cap_usd,
            passed=True,
            failure_reason=None,
        )
