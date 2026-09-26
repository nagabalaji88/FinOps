"""Investment research tools: market data, filings, portfolio and macro analysis.

Live sources are used when credentials are present (Alpha Vantage for prices, NewsAPI
for news, SEC EDGAR for filings -- EDGAR needs only a declared User-Agent). Otherwise
the tools fall back to the instrument and price data held in the platform database and
say so explicitly in the ``source`` field of every response.
"""

from __future__ import annotations

import statistics
from datetime import UTC, date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from app.core.config import settings
from app.core.errors import NotFoundError, ProviderNotConfiguredError, ValidationError
from app.db.models.banking import Holding, Portfolio, PriceBar, ResearchNote, Security
from app.llm.base import http_client
from app.tools.base import ToolContext, tool


class QuoteArgs(BaseModel):
    symbol: str
    days: int = Field(default=90, ge=5, le=1825)


@tool(
    "get_market_data",
    "Fetch price history and computed statistics (returns, volatility, drawdown, moving averages).",
    QuoteArgs,
    category="market",
    timeout_seconds=45,
)
async def get_market_data(args: QuoteArgs, ctx: ToolContext) -> dict[str, Any]:
    symbol = args.symbol.upper()
    bars: list[dict[str, Any]] = []
    source = "database"

    if settings.market_data_api_key:
        resp = await http_client().get(
            f"{settings.market_data_base_url}/query",
            params={
                "function": "TIME_SERIES_DAILY",
                "symbol": symbol,
                "outputsize": "compact",
                "apikey": settings.market_data_api_key,
            },
            timeout=30.0,
        )
        payload = resp.json() if resp.status_code < 400 else {}
        series = payload.get("Time Series (Daily)") or {}
        for day, values in sorted(series.items())[-args.days :]:
            bars.append(
                {
                    "date": day,
                    "open": float(values["1. open"]),
                    "high": float(values["2. high"]),
                    "low": float(values["3. low"]),
                    "close": float(values["4. close"]),
                    "volume": float(values["5. volume"]),
                }
            )
        if bars:
            source = "alphavantage"

    if not bars:
        since = date.today() - timedelta(days=args.days * 2)
        rows = (
            (
                await ctx.session.execute(
                    select(PriceBar)
                    .where(PriceBar.symbol == symbol, PriceBar.bar_date >= since)
                    .order_by(PriceBar.bar_date)
                )
            )
            .scalars()
            .all()
        )
        bars = [
            {
                "date": r.bar_date.isoformat(),
                "open": r.open,
                "high": r.high,
                "low": r.low,
                "close": r.close,
                "volume": r.volume,
            }
            for r in rows[-args.days :]
        ]
    if not bars:
        raise NotFoundError(
            f"No price history available for '{symbol}'",
            details={"hint": "Configure MARKET_DATA_API_KEY or load price bars for this symbol"},
        )

    closes = [b["close"] for b in bars]
    returns = [(closes[i] / closes[i - 1]) - 1 for i in range(1, len(closes))]
    peak = closes[0]
    max_drawdown = 0.0
    for price in closes:
        peak = max(peak, price)
        max_drawdown = min(max_drawdown, price / peak - 1)
    volatility = statistics.pstdev(returns) * (252**0.5) if len(returns) > 1 else 0.0

    security = (
        await ctx.session.execute(select(Security).where(Security.symbol == symbol))
    ).scalar_one_or_none()
    if security is not None:
        security.last_price = closes[-1]
        security.last_price_at = datetime.now(UTC)
        await ctx.session.flush()

    return {
        "symbol": symbol,
        "source": source,
        "name": security.name if security else None,
        "sector": security.sector if security else None,
        "currency": security.currency if security else None,
        "bars": bars[-120:],
        "statistics": {
            "last_close": round(closes[-1], 4),
            "period_return_pct": round((closes[-1] / closes[0] - 1) * 100, 3),
            "annualised_volatility_pct": round(volatility * 100, 3),
            "max_drawdown_pct": round(max_drawdown * 100, 3),
            "sma_20": round(statistics.fmean(closes[-20:]), 4) if len(closes) >= 20 else None,
            "sma_50": round(statistics.fmean(closes[-50:]), 4) if len(closes) >= 50 else None,
            "high_52w": round(max(b["high"] for b in bars), 4),
            "low_52w": round(min(b["low"] for b in bars), 4),
            "observations": len(bars),
        },
    }


class NewsArgs(BaseModel):
    query: str
    limit: int = Field(default=10, ge=1, le=50)
    days: int = Field(default=14, ge=1, le=90)


@tool(
    "get_market_news",
    "Retrieve recent market and company news headlines from the configured news provider.",
    NewsArgs,
    category="market",
    timeout_seconds=45,
)
async def get_market_news(args: NewsArgs, ctx: ToolContext) -> dict[str, Any]:
    if not settings.news_api_key:
        raise ProviderNotConfiguredError(
            "News provider is not configured",
            details={"required": "NEWS_API_KEY", "provider": settings.news_base_url},
        )
    since = (datetime.now(UTC) - timedelta(days=args.days)).date().isoformat()
    resp = await http_client().get(
        f"{settings.news_base_url}/everything",
        params={
            "q": args.query,
            "from": since,
            "sortBy": "publishedAt",
            "pageSize": args.limit,
            "language": "en",
        },
        headers={"X-Api-Key": settings.news_api_key},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise ProviderNotConfiguredError(
            f"News provider error {resp.status_code}", details={"body": resp.text[:400]}
        )
    articles = resp.json().get("articles", [])
    return {
        "query": args.query,
        "source": "newsapi",
        "count": len(articles),
        "articles": [
            {
                "title": a.get("title"),
                "source": (a.get("source") or {}).get("name"),
                "published_at": a.get("publishedAt"),
                "url": a.get("url"),
                "description": a.get("description"),
                "author": a.get("author"),
            }
            for a in articles
        ],
    }


class FilingArgs(BaseModel):
    symbol: str | None = None
    cik: str | None = None
    form_types: list[str] = Field(default_factory=lambda: ["10-K", "10-Q", "8-K"])
    limit: int = Field(default=10, ge=1, le=50)


@tool(
    "get_company_filings",
    "Retrieve recent SEC EDGAR filings for a company by ticker or CIK.",
    FilingArgs,
    category="market",
    timeout_seconds=60,
)
async def get_company_filings(args: FilingArgs, ctx: ToolContext) -> dict[str, Any]:
    cik = args.cik
    if not cik and args.symbol:
        security = (
            await ctx.session.execute(select(Security).where(Security.symbol == args.symbol.upper()))
        ).scalar_one_or_none()
        cik = security.cik if security else None
        if not cik:
            resp = await http_client().get(
                "https://www.sec.gov/files/company_tickers.json",
                headers={"User-Agent": settings.sec_edgar_user_agent},
                timeout=30.0,
            )
            if resp.status_code < 400:
                for entry in resp.json().values():
                    if entry.get("ticker", "").upper() == args.symbol.upper():
                        cik = str(entry["cik_str"])
                        break
    if not cik:
        raise NotFoundError(
            "Unable to resolve a CIK for the requested company", details={"symbol": args.symbol}
        )

    padded = str(cik).zfill(10)
    resp = await http_client().get(
        f"https://data.sec.gov/submissions/CIK{padded}.json",
        headers={"User-Agent": settings.sec_edgar_user_agent},
        timeout=45.0,
    )
    if resp.status_code >= 400:
        raise ProviderNotConfiguredError(
            f"SEC EDGAR returned {resp.status_code}",
            details={"hint": "Set SEC_EDGAR_USER_AGENT to a contactable value per SEC policy"},
        )
    data = resp.json()
    recent = (data.get("filings") or {}).get("recent") or {}
    filings: list[dict[str, Any]] = []
    for i, form in enumerate(recent.get("form", [])):
        if args.form_types and form not in args.form_types:
            continue
        accession = recent["accessionNumber"][i].replace("-", "")
        filings.append(
            {
                "form": form,
                "filed_at": recent["filingDate"][i],
                "period": recent.get("reportDate", [None] * (i + 1))[i],
                "accession_number": recent["accessionNumber"][i],
                "primary_document": recent["primaryDocument"][i],
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/"
                f"{recent['primaryDocument'][i]}",
            }
        )
        if len(filings) >= args.limit:
            break
    return {
        "company": data.get("name"),
        "cik": cik,
        "source": "sec_edgar",
        "sic_description": data.get("sicDescription"),
        "count": len(filings),
        "filings": filings,
    }


class PortfolioArgs(BaseModel):
    portfolio_code: str


@tool(
    "analyse_portfolio",
    "Value a portfolio, compute allocation, concentration and P&L against cost basis.",
    PortfolioArgs,
    category="market",
    timeout_seconds=60,
)
async def analyse_portfolio(args: PortfolioArgs, ctx: ToolContext) -> dict[str, Any]:
    portfolio = (
        await ctx.session.execute(select(Portfolio).where(Portfolio.portfolio_code == args.portfolio_code))
    ).scalar_one_or_none()
    if portfolio is None:
        raise NotFoundError(f"Portfolio '{args.portfolio_code}' not found")
    holdings = (
        (await ctx.session.execute(select(Holding).where(Holding.portfolio_id == portfolio.id)))
        .scalars()
        .all()
    )
    if not holdings:
        return {
            "portfolio_code": portfolio.portfolio_code,
            "holdings": [],
            "market_value": portfolio.cash_balance,
            "message": "No holdings",
        }

    symbols = [h.symbol for h in holdings]
    securities = {
        s.symbol: s
        for s in (await ctx.session.execute(select(Security).where(Security.symbol.in_(symbols))))
        .scalars()
        .all()
    }
    latest_prices: dict[str, float] = {}
    for symbol in symbols:
        bar = (
            await ctx.session.execute(
                select(PriceBar).where(PriceBar.symbol == symbol).order_by(PriceBar.bar_date.desc()).limit(1)
            )
        ).scalar_one_or_none()
        security = securities.get(symbol)
        latest_prices[symbol] = (
            bar.close if bar else (security.last_price if security and security.last_price else 0.0)
        )

    rows: list[dict[str, Any]] = []
    total_value = portfolio.cash_balance
    total_cost = portfolio.cash_balance
    by_sector: dict[str, float] = {}
    by_asset_class: dict[str, float] = {}
    for h in holdings:
        price = latest_prices.get(h.symbol, 0.0)
        value = price * h.quantity
        cost = h.average_cost * h.quantity
        total_value += value
        total_cost += cost
        sector = (
            h.sector
            or (securities.get(h.symbol).sector if securities.get(h.symbol) else "Unclassified")
            or "Unclassified"
        )
        by_sector[sector] = by_sector.get(sector, 0.0) + value
        by_asset_class[h.asset_class] = by_asset_class.get(h.asset_class, 0.0) + value
        rows.append(
            {
                "symbol": h.symbol,
                "name": securities.get(h.symbol).name if securities.get(h.symbol) else None,
                "quantity": h.quantity,
                "average_cost": round(h.average_cost, 4),
                "last_price": round(price, 4),
                "market_value": round(value, 2),
                "cost_basis": round(cost, 2),
                "unrealised_pnl": round(value - cost, 2),
                "unrealised_pnl_pct": round((value / cost - 1) * 100, 3) if cost else None,
                "sector": sector,
                "asset_class": h.asset_class,
            }
        )

    for row in rows:
        row["weight_pct"] = round(row["market_value"] / total_value * 100, 3) if total_value else 0
    rows.sort(key=lambda r: r["market_value"], reverse=True)
    weights = [r["weight_pct"] / 100 for r in rows]
    hhi = sum(w**2 for w in weights)

    return {
        "portfolio_code": portfolio.portfolio_code,
        "name": portfolio.name,
        "strategy": portfolio.strategy,
        "benchmark": portfolio.benchmark,
        "risk_profile": portfolio.risk_profile,
        "base_currency": portfolio.base_currency,
        "cash_balance": round(portfolio.cash_balance, 2),
        "market_value": round(total_value, 2),
        "cost_basis": round(total_cost, 2),
        "unrealised_pnl": round(total_value - total_cost, 2),
        "unrealised_pnl_pct": round((total_value / total_cost - 1) * 100, 3) if total_cost else None,
        "holdings": rows,
        "allocation_by_sector": {
            k: round(v / total_value * 100, 3) for k, v in sorted(by_sector.items(), key=lambda kv: -kv[1])
        },
        "allocation_by_asset_class": {k: round(v / total_value * 100, 3) for k, v in by_asset_class.items()},
        "concentration": {
            "herfindahl_index": round(hhi, 4),
            "top_holding_pct": rows[0]["weight_pct"] if rows else 0,
            "top_5_pct": round(sum(r["weight_pct"] for r in rows[:5]), 3),
            "assessment": "concentrated" if hhi > 0.25 else "diversified",
        },
    }


class RiskArgs(BaseModel):
    portfolio_code: str
    confidence: float = Field(default=0.95, ge=0.8, le=0.99)
    days: int = Field(default=252, ge=60, le=1260)


@tool(
    "analyse_portfolio_risk",
    "Compute historical VaR, expected shortfall, beta and volatility for a portfolio.",
    RiskArgs,
    category="market",
    timeout_seconds=90,
)
async def analyse_portfolio_risk(args: RiskArgs, ctx: ToolContext) -> dict[str, Any]:
    portfolio = (
        await ctx.session.execute(select(Portfolio).where(Portfolio.portfolio_code == args.portfolio_code))
    ).scalar_one_or_none()
    if portfolio is None:
        raise NotFoundError(f"Portfolio '{args.portfolio_code}' not found")
    holdings = (
        (await ctx.session.execute(select(Holding).where(Holding.portfolio_id == portfolio.id)))
        .scalars()
        .all()
    )
    if not holdings:
        raise ValidationError("Portfolio has no holdings to analyse")

    since = date.today() - timedelta(days=int(args.days * 1.6))
    series: dict[str, dict[str, float]] = {}
    for h in holdings:
        bars = (
            (
                await ctx.session.execute(
                    select(PriceBar)
                    .where(PriceBar.symbol == h.symbol, PriceBar.bar_date >= since)
                    .order_by(PriceBar.bar_date)
                )
            )
            .scalars()
            .all()
        )
        if bars:
            series[h.symbol] = {b.bar_date.isoformat(): b.close for b in bars}
    if not series:
        raise NotFoundError("No price history available for the portfolio constituents")

    common_dates = sorted(set.intersection(*(set(v) for v in series.values())))
    if len(common_dates) < 30:
        raise ValidationError(
            "Insufficient overlapping price history (need at least 30 days)",
            details={"available_days": len(common_dates)},
        )

    quantities = {h.symbol: h.quantity for h in holdings}
    values = [sum(series[s][d] * quantities.get(s, 0.0) for s in series) for d in common_dates]
    returns = [(values[i] / values[i - 1]) - 1 for i in range(1, len(values)) if values[i - 1]]
    returns.sort()
    idx = max(int((1 - args.confidence) * len(returns)) - 1, 0)
    var_pct = returns[idx] if returns else 0.0
    tail = returns[: idx + 1] or [0.0]
    es_pct = statistics.fmean(tail)
    vol = statistics.pstdev(returns) * (252**0.5) if len(returns) > 1 else 0.0
    latest_value = values[-1]

    benchmark_beta = None
    bench_bars = (
        (
            await ctx.session.execute(
                select(PriceBar)
                .where(PriceBar.symbol == portfolio.benchmark, PriceBar.bar_date >= since)
                .order_by(PriceBar.bar_date)
            )
        )
        .scalars()
        .all()
    )
    if len(bench_bars) > 30:
        bench = {b.bar_date.isoformat(): b.close for b in bench_bars}
        paired = [(d, bench[d]) for d in common_dates if d in bench]
        if len(paired) > 30:
            bench_values = [p[1] for p in paired]
            bench_returns = [(bench_values[i] / bench_values[i - 1]) - 1 for i in range(1, len(bench_values))]
            port_returns = [(values[i] / values[i - 1]) - 1 for i in range(1, len(paired))]
            n = min(len(bench_returns), len(port_returns))
            if n > 5:
                cov = statistics.fmean(
                    [
                        (port_returns[i] - statistics.fmean(port_returns[:n]))
                        * (bench_returns[i] - statistics.fmean(bench_returns[:n]))
                        for i in range(n)
                    ]
                )
                bench_var = statistics.pvariance(bench_returns[:n])
                benchmark_beta = round(cov / bench_var, 4) if bench_var else None

    return {
        "portfolio_code": portfolio.portfolio_code,
        "observations": len(returns),
        "confidence": args.confidence,
        "market_value": round(latest_value, 2),
        "annualised_volatility_pct": round(vol * 100, 3),
        "value_at_risk_pct": round(var_pct * 100, 3),
        "value_at_risk_amount": round(abs(var_pct) * latest_value, 2),
        "expected_shortfall_pct": round(es_pct * 100, 3),
        "expected_shortfall_amount": round(abs(es_pct) * latest_value, 2),
        "beta_vs_benchmark": benchmark_beta,
        "benchmark": portfolio.benchmark,
        "method": "historical simulation",
    }


class SectorArgs(BaseModel):
    sector: str | None = None
    symbols: list[str] = Field(default_factory=list)
    days: int = Field(default=180, ge=30, le=1095)


@tool(
    "compare_sector",
    "Compare instruments within a sector on return, volatility and valuation fundamentals.",
    SectorArgs,
    category="market",
    timeout_seconds=90,
)
async def compare_sector(args: SectorArgs, ctx: ToolContext) -> dict[str, Any]:
    stmt = select(Security)
    if args.symbols:
        stmt = stmt.where(Security.symbol.in_([s.upper() for s in args.symbols]))
    elif args.sector:
        stmt = stmt.where(Security.sector == args.sector)
    else:
        raise ValidationError("Provide either a sector or a list of symbols")
    securities = (await ctx.session.execute(stmt)).scalars().all()
    if not securities:
        raise NotFoundError("No instruments matched the request")

    since = date.today() - timedelta(days=args.days)
    rows = []
    for security in securities:
        bars = (
            (
                await ctx.session.execute(
                    select(PriceBar)
                    .where(PriceBar.symbol == security.symbol, PriceBar.bar_date >= since)
                    .order_by(PriceBar.bar_date)
                )
            )
            .scalars()
            .all()
        )
        closes = [b.close for b in bars]
        returns = [(closes[i] / closes[i - 1]) - 1 for i in range(1, len(closes))]
        rows.append(
            {
                "symbol": security.symbol,
                "name": security.name,
                "sector": security.sector,
                "industry": security.industry,
                "last_close": round(closes[-1], 4) if closes else security.last_price,
                "period_return_pct": round((closes[-1] / closes[0] - 1) * 100, 3)
                if len(closes) > 1
                else None,
                "annualised_volatility_pct": round(statistics.pstdev(returns) * (252**0.5) * 100, 3)
                if len(returns) > 1
                else None,
                "fundamentals": security.fundamentals,
                "observations": len(closes),
            }
        )
    ranked = sorted(
        [r for r in rows if r["period_return_pct"] is not None],
        key=lambda r: r["period_return_pct"],
        reverse=True,
    )
    return {
        "sector": args.sector,
        "window_days": args.days,
        "instrument_count": len(rows),
        "instruments": rows,
        "best_performer": ranked[0] if ranked else None,
        "worst_performer": ranked[-1] if ranked else None,
        "median_return_pct": round(statistics.median([r["period_return_pct"] for r in ranked]), 3)
        if ranked
        else None,
    }


class FinancialsArgs(BaseModel):
    symbol: str


@tool(
    "analyse_financial_statements",
    "Analyse stored fundamentals: growth, margins, leverage, returns and valuation ratios.",
    FinancialsArgs,
    category="market",
)
async def analyse_financial_statements(args: FinancialsArgs, ctx: ToolContext) -> dict[str, Any]:
    security = (
        await ctx.session.execute(select(Security).where(Security.symbol == args.symbol.upper()))
    ).scalar_one_or_none()
    if security is None:
        raise NotFoundError(f"Security '{args.symbol}' is not in the instrument master")
    f = security.fundamentals or {}
    if not f:
        raise NotFoundError(
            f"No fundamentals stored for '{args.symbol}'",
            details={"hint": "Load fundamentals via the instrument master or a data vendor"},
        )

    def ratio(numerator: str, denominator: str) -> float | None:
        n, d = f.get(numerator), f.get(denominator)
        if n is None or not d:
            return None
        return round(n / d, 4)

    analysis = {
        "symbol": security.symbol,
        "name": security.name,
        "currency": security.currency,
        "period": f.get("period"),
        "profitability": {
            "gross_margin": ratio("gross_profit", "revenue"),
            "operating_margin": ratio("operating_income", "revenue"),
            "net_margin": ratio("net_income", "revenue"),
            "return_on_equity": ratio("net_income", "total_equity"),
            "return_on_assets": ratio("net_income", "total_assets"),
        },
        "growth": {
            "revenue_growth": round(f["revenue"] / f["revenue_prior"] - 1, 4)
            if f.get("revenue") and f.get("revenue_prior")
            else None,
            "earnings_growth": round(f["net_income"] / f["net_income_prior"] - 1, 4)
            if f.get("net_income") and f.get("net_income_prior")
            else None,
        },
        "leverage": {
            "debt_to_equity": ratio("total_debt", "total_equity"),
            "interest_coverage": ratio("operating_income", "interest_expense"),
            "current_ratio": ratio("current_assets", "current_liabilities"),
        },
        "valuation": {
            "pe_ratio": f.get("pe_ratio"),
            "price_to_book": f.get("price_to_book"),
            "ev_to_ebitda": f.get("ev_to_ebitda"),
            "dividend_yield": f.get("dividend_yield"),
            "market_cap": f.get("market_cap"),
        },
        "raw_fundamentals": f,
    }
    flags = []
    lev = analysis["leverage"]["debt_to_equity"]
    if lev is not None and lev > 2:
        flags.append("Leverage above 2x equity")
    cover = analysis["leverage"]["interest_coverage"]
    if cover is not None and cover < 3:
        flags.append("Interest coverage below 3x")
    margin = analysis["profitability"]["net_margin"]
    if margin is not None and margin < 0:
        flags.append("Negative net margin")
    analysis["risk_flags"] = flags
    return analysis


class MacroArgs(BaseModel):
    region: str = Field(default="IN")
    indicators: list[str] = Field(
        default_factory=lambda: ["policy_rate", "cpi_inflation", "gdp_growth", "unemployment"]
    )


@tool(
    "get_macro_indicators",
    "Fetch macroeconomic indicators for a region from the World Bank open data API.",
    MacroArgs,
    category="market",
    timeout_seconds=60,
)
async def get_macro_indicators(args: MacroArgs, ctx: ToolContext) -> dict[str, Any]:
    mapping = {
        "cpi_inflation": "FP.CPI.TOTL.ZG",
        "gdp_growth": "NY.GDP.MKTP.KD.ZG",
        "unemployment": "SL.UEM.TOTL.ZS",
        "policy_rate": "FR.INR.RINR",
        "current_account": "BN.CAB.XOKA.GD.ZS",
        "government_debt": "GC.DOD.TOTL.GD.ZS",
    }
    results: dict[str, Any] = {}
    for indicator in args.indicators:
        code = mapping.get(indicator)
        if not code:
            results[indicator] = {"error": "unknown indicator", "supported": sorted(mapping)}
            continue
        resp = await http_client().get(
            f"https://api.worldbank.org/v2/country/{args.region}/indicator/{code}",
            params={"format": "json", "per_page": 8, "mrnev": 5},
            timeout=30.0,
        )
        if resp.status_code >= 400:
            results[indicator] = {"error": f"world bank returned {resp.status_code}"}
            continue
        payload = resp.json()
        observations = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        results[indicator] = {
            "code": code,
            "series": [
                {"year": o.get("date"), "value": o.get("value")}
                for o in observations
                if o.get("value") is not None
            ],
            "latest": next((o.get("value") for o in observations if o.get("value") is not None), None),
        }
    return {
        "region": args.region,
        "source": "worldbank",
        "indicators": results,
        "retrieved_at": datetime.now(UTC).isoformat(),
    }


class NoteArgs(BaseModel):
    symbol: str | None = None
    title: str
    thesis: str
    recommendation: str = Field(description="buy|hold|sell|overweight|underweight")
    target_price: float | None = None
    horizon_months: int = Field(default=12, ge=1, le=60)
    conviction: str = Field(default="medium")
    risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)


@tool(
    "publish_research_note",
    "Persist an investment research note with recommendation, risks and citations. "
    "Requires human approval before publication.",
    NoteArgs,
    category="market",
    writes_data=True,
    idempotent=False,
    requires_approval=True,
    approval_risk="high",
)
async def publish_research_note(args: NoteArgs, ctx: ToolContext) -> dict[str, Any]:
    if len(args.thesis) < 150:
        raise ValidationError("Research thesis must be at least 150 characters")
    if not args.risks:
        raise ValidationError("At least one downside risk must be documented")
    note = ResearchNote(
        symbol=args.symbol.upper() if args.symbol else None,
        title=args.title[:300],
        thesis=args.thesis,
        recommendation=args.recommendation,
        target_price=args.target_price,
        horizon_months=args.horizon_months,
        conviction=args.conviction,
        risks=args.risks,
        catalysts=args.catalysts,
        citations=args.citations,
        analyst=ctx.user_email or "ai_agent",
        execution_id=ctx.execution_id,
    )
    ctx.session.add(note)
    await ctx.session.flush()
    return {
        "note_id": note.id,
        "symbol": note.symbol,
        "recommendation": note.recommendation,
        "target_price": note.target_price,
        "published_at": note.created_at.isoformat(),
    }
