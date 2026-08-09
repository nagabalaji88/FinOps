/**
 * Geography — where the book actually sits.
 *
 * Every figure on this screen is aggregated from the banking ledger by
 * `GET /dashboard/geography`: transaction value by counterparty jurisdiction, customer
 * locations from the customer master, corridors from the customer's home city to the
 * counterparty country, and jurisdiction risk from the AML rule set and loaded watchlists.
 */
import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion } from 'framer-motion'
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as ReTooltip,
  XAxis,
  YAxis,
} from 'recharts'
import {
  ArrowsRightLeftIcon,
  BuildingOffice2Icon,
  ExclamationTriangleIcon,
  GlobeAltIcon,
  MapPinIcon,
} from '@heroicons/react/24/outline'
import { api, AnimatedNumber, Badge, Card, CardHeader, ChartTooltip, EmptyState, ErrorState, Meter, PageHeader, Reveal, SkeletonCard, Stagger, Tabs, TabPanel, formatCompact, formatNumber, formatPercent, relativeTime } from '@finops/shared'
import { Globe, type GlobeArc, type GlobeMarker, type RiskLevel } from '@/components/geo/Globe'
import { ShareGauge } from '@/components/geo/ShareGauge'

interface GeoCountry {
  code: string
  name: string
  latitude: number
  longitude: number
  region: string
  domestic: boolean
  risk_level: RiskLevel
  transactions: number
  total_value: number
  inbound_value: number
  outbound_value: number
  flagged: number
  counterparty_customers: number
  resident_customers: number
  securities: number
  watchlist_entries: number
  watchlist_peps: number
  share_pct: number
}

interface GeoCity {
  name: string
  latitude: number
  longitude: number
  country: string
  customers: number
  high_risk_customers: number
  transactions: number
  total_value: number
  cross_border_value: number
  flagged: number
  alerts: number
  share_pct: number
}

interface GeoCorridor {
  from_city: string
  from_latitude: number
  from_longitude: number
  to_code: string
  to_country: string
  to_latitude: number
  to_longitude: number
  risk_level: RiskLevel
  transactions: number
  total_value: number
  outbound_value: number
  inbound_value: number
  flagged: number
}

interface GeographyData {
  generated_at: string
  window_days: number
  summary: {
    countries: number
    cities: number
    corridors: number
    customers: number
    transactions: number
    total_value: number
    currency: string
    domestic_country: string
    cross_border_transactions: number
    cross_border_value: number
    cross_border_share_pct: number
    high_risk_transactions: number
    high_risk_value: number
    high_risk_share_pct: number
    flagged_transactions: number
    alerts: number
  }
  countries: GeoCountry[]
  cities: GeoCity[]
  corridors: GeoCorridor[]
  trend: {
    date: string
    transactions: number
    domestic_value: number
    cross_border_value: number
    high_risk_value: number
  }[]
  unmapped: string[]
}

const WINDOWS = [30, 90, 180, 365]

const RISK_TONE: Record<RiskLevel, 'ok' | 'warn' | 'err' | 'info' | 'idle'> = {
  high: 'err',
  elevated: 'warn',
  standard: 'info',
  domestic: 'ok',
}

const RISK_COLOUR: Record<RiskLevel, string> = {
  high: 'rgb(var(--state-err))',
  elevated: 'rgb(var(--state-warn))',
  standard: 'rgb(var(--state-info))',
  domestic: 'rgb(var(--state-ok))',
}

const RISK_LABEL: Record<RiskLevel, string> = {
  high: 'High risk',
  elevated: 'Watchlist presence',
  standard: 'Standard',
  domestic: 'Domestic',
}

const chartAxis = {
  stroke: 'rgb(var(--line))',
  tick: { fill: 'rgb(var(--ink-subtle))', fontSize: 10 },
  tickLine: false,
  axisLine: false,
}

function money(value: number, currency: string): string {
  return `${currency} ${formatCompact(value)}`
}

/** A headline figure that counts up and carries its own share meter. */
function GeoStat({
  label,
  value,
  format,
  hint,
  share,
  tone = 'idle',
  icon,
}: {
  label: string
  value: number
  format: (value: number) => string
  hint?: string
  share?: number
  tone?: 'ok' | 'warn' | 'err' | 'info' | 'idle'
  icon: React.ReactNode
}) {
  const toneColor = {
    ok: 'text-state-ok',
    warn: 'text-state-warn',
    err: 'text-state-err',
    info: 'text-state-info',
    idle: 'text-ink',
  }[tone]
  return (
    <Reveal>
      <Card className="h-full" interactive>
        <div className="flex flex-col gap-2 px-5 py-4">
          <div className="flex items-center justify-between gap-2">
            <span className="metric-label">{label}</span>
            <span className="text-ink-subtle">{icon}</span>
          </div>
          <AnimatedNumber value={value} format={format} className={`metric-value ${toneColor}`} />
          {hint ? <span className="text-xs text-ink-muted">{hint}</span> : null}
          {share !== undefined ? (
            <Meter value={share} tone={tone === 'idle' ? 'info' : tone} className="mt-1" />
          ) : null}
        </div>
      </Card>
    </Reveal>
  )
}

export default function Geography() {
  const [days, setDays] = useState(90)
  const [tab, setTab] = useState('countries')
  const [selected, setSelected] = useState<string | null>(null)

  const query = useQuery({
    queryKey: ['geography', days],
    queryFn: () => api.get<GeographyData>('/dashboard/geography', { days }),
    refetchInterval: 120_000,
  })

  const data = query.data

  const markers = useMemo<GlobeMarker[]>(() => {
    if (!data) return []
    const currency = data.summary.currency
    const countryMarkers = data.countries.map((country) => ({
      id: `country:${country.code}`,
      name: country.name,
      latitude: country.latitude,
      longitude: country.longitude,
      kind: 'country' as const,
      risk: country.domestic ? ('domestic' as RiskLevel) : country.risk_level,
      share: country.share_pct,
      primary: `${money(country.total_value, currency)} · ${formatNumber(country.transactions)} txns`,
      secondary: `${RISK_LABEL[country.domestic ? 'domestic' : country.risk_level]} · ${formatPercent(country.share_pct)} of value`,
    }))
    const cityMarkers = data.cities.map((city) => ({
      id: `city:${city.name}`,
      name: city.name,
      latitude: city.latitude,
      longitude: city.longitude,
      kind: 'city' as const,
      risk: 'domestic' as RiskLevel,
      share: city.share_pct,
      primary: `${formatNumber(city.customers)} customers · ${money(city.total_value, currency)}`,
      secondary: `${formatNumber(city.transactions)} transactions · ${formatNumber(city.alerts)} alerts`,
    }))
    return [...countryMarkers, ...cityMarkers]
  }, [data])

  const arcs = useMemo<GlobeArc[]>(() => {
    if (!data) return []
    const peak = Math.max(...data.corridors.map((corridor) => corridor.total_value), 1)
    return data.corridors.slice(0, 24).map((corridor) => ({
      id: `corridor:${corridor.from_city}:${corridor.to_code}`,
      from: {
        latitude: corridor.from_latitude,
        longitude: corridor.from_longitude,
      },
      to: { latitude: corridor.to_latitude, longitude: corridor.to_longitude },
      risk: corridor.risk_level,
      weight: corridor.total_value / peak,
      label: `${corridor.from_city} → ${corridor.to_country}`,
    }))
  }, [data])

  /** Where the globe should face on load: the bank's domestic market. */
  const home = useMemo(() => {
    const domestic = data?.countries.find((country) => country.domestic) ?? data?.countries[0]
    return domestic ? { latitude: domestic.latitude, longitude: domestic.longitude } : undefined
  }, [data])

  const detail = useMemo(() => {
    if (!data || !selected) return null
    const [kind, key] = selected.split(':')
    if (kind === 'country') {
      const country = data.countries.find((item) => item.code === key)
      if (!country) return null
      const risk: RiskLevel = country.domestic ? 'domestic' : country.risk_level
      return {
        title: country.name,
        subtitle: `${country.region} · ${country.code}`,
        badge: RISK_LABEL[risk],
        tone: RISK_TONE[risk],
        rows: [
          ['Transaction value', money(country.total_value, data.summary.currency)],
          ['Share of book', formatPercent(country.share_pct)],
          ['Transactions', formatNumber(country.transactions)],
          ['Inbound', money(country.inbound_value, data.summary.currency)],
          ['Outbound', money(country.outbound_value, data.summary.currency)],
          ['Counterparty customers', formatNumber(country.counterparty_customers)],
          ['Resident customers', formatNumber(country.resident_customers)],
          ['Flagged transactions', formatNumber(country.flagged)],
          ['Instruments listed', formatNumber(country.securities)],
          [
            'Watchlist entries',
            `${formatNumber(country.watchlist_entries)} (${formatNumber(country.watchlist_peps)} PEP)`,
          ],
        ] as [string, string][],
      }
    }
    const city = data.cities.find((item) => item.name === key)
    if (!city) return null
    return {
      title: city.name,
      subtitle: `${city.country} · customer location`,
      badge: city.high_risk_customers ? `${city.high_risk_customers} high-risk` : 'Domestic',
      tone: (city.high_risk_customers ? 'warn' : 'ok') as 'warn' | 'ok',
      rows: [
        ['Customers', formatNumber(city.customers)],
        ['High-risk customers', formatNumber(city.high_risk_customers)],
        ['Transaction value', money(city.total_value, data.summary.currency)],
        ['Cross-border value', money(city.cross_border_value, data.summary.currency)],
        ['Transactions', formatNumber(city.transactions)],
        ['Flagged transactions', formatNumber(city.flagged)],
        ['AML alerts', formatNumber(city.alerts)],
        ['Share of book', formatPercent(city.share_pct)],
      ] as [string, string][],
    }
  }, [data, selected])

  if (query.isError) return <ErrorState error={query.error} retry={() => query.refetch()} />

  const currency = data?.summary.currency ?? 'INR'
  const trend =
    data?.trend.map((point) => ({
      ...point,
      label: new Date(point.date).toLocaleDateString('en-GB', {
        day: '2-digit',
        month: 'short',
      }),
    })) ?? []

  return (
    <div className="space-y-6">
      <PageHeader
        title="Geography"
        description="Transaction corridors, customer locations and jurisdiction risk, aggregated from the banking ledger."
        actions={
          <>
            <div className="flex items-center gap-1 rounded-xl border border-line/70 bg-surface-muted/60 p-1">
              {WINDOWS.map((window) => (
                <button
                  key={window}
                  type="button"
                  onClick={() => setDays(window)}
                  className="relative rounded-lg px-3 py-1.5 text-xs font-medium transition-colors"
                >
                  {days === window ? (
                    <motion.span
                      layoutId="geo-window"
                      className="absolute inset-0 rounded-lg bg-surface-raised shadow-glass"
                      transition={{
                        type: 'spring',
                        stiffness: 380,
                        damping: 32,
                      }}
                    />
                  ) : null}
                  <span className={days === window ? 'relative text-ink' : 'relative text-ink-muted'}>
                    {window}d
                  </span>
                </button>
              ))}
            </div>
            {data ? (
              <Badge tone="idle" dot>
                Updated {relativeTime(data.generated_at)}
              </Badge>
            ) : null}
          </>
        }
      />

      {!data ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {[0, 1, 2, 3].map((index) => (
            <SkeletonCard key={index} rows={2} />
          ))}
        </div>
      ) : (
        <>
          <Stagger className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <GeoStat
              label="Jurisdictions touched"
              value={data.summary.countries}
              format={(value) => formatNumber(Math.round(value))}
              hint={`${formatNumber(data.summary.transactions)} transactions over ${data.window_days} days`}
              icon={<GlobeAltIcon className="h-4 w-4" />}
            />
            <GeoStat
              label="Cross-border value"
              value={data.summary.cross_border_value}
              format={(value) => money(value, currency)}
              hint={`${formatNumber(data.summary.cross_border_transactions)} transactions off-shore`}
              share={data.summary.cross_border_share_pct}
              tone="info"
              icon={<ArrowsRightLeftIcon className="h-4 w-4" />}
            />
            <GeoStat
              label="High-risk jurisdictions"
              value={data.summary.high_risk_value}
              format={(value) => money(value, currency)}
              hint={`${formatNumber(data.summary.high_risk_transactions)} transactions under FATF-listed corridors`}
              share={data.summary.high_risk_share_pct}
              tone={data.summary.high_risk_value > 0 ? 'err' : 'ok'}
              icon={<ExclamationTriangleIcon className="h-4 w-4" />}
            />
            <GeoStat
              label="Customer locations"
              value={data.summary.cities}
              format={(value) => formatNumber(Math.round(value))}
              hint={`${formatNumber(data.summary.customers)} customers · ${formatNumber(data.summary.alerts)} alerts`}
              icon={<BuildingOffice2Icon className="h-4 w-4" />}
            />
          </Stagger>

          <div className="grid gap-4 xl:grid-cols-[minmax(0,1.55fr)_minmax(0,1fr)]">
            <Card className="flex flex-col overflow-hidden">
              <CardHeader
                title="Global activity"
                subtitle="Drag to rotate. Marker size is share of transaction value; arcs are live corridors."
                action={
                  <div className="hidden items-center gap-3 sm:flex">
                    {(['domestic', 'standard', 'elevated', 'high'] as RiskLevel[]).map((risk) => (
                      <span key={risk} className="flex items-center gap-1.5 text-2xs text-ink-muted">
                        <span className="h-2 w-2 rounded-full" style={{ background: RISK_COLOUR[risk] }} />
                        {RISK_LABEL[risk]}
                      </span>
                    ))}
                  </div>
                }
              />
              <Globe
                markers={markers}
                arcs={arcs}
                selectedId={selected}
                onSelect={setSelected}
                initialCentre={home}
                className="m-auto aspect-square w-full max-w-[560px] px-4 py-2"
              />
            </Card>

            <div className="space-y-4">
              <Card>
                <CardHeader
                  title={detail ? detail.title : 'Location detail'}
                  subtitle={detail ? detail.subtitle : 'Select a marker on the globe or a row below.'}
                  action={detail ? <Badge tone={detail.tone}>{detail.badge}</Badge> : null}
                />
                <div className="px-5 pb-5 pt-4">
                  <AnimatePresence mode="wait">
                    {detail ? (
                      <motion.dl
                        key={detail.title}
                        initial={{ opacity: 0, y: 8 }}
                        animate={{ opacity: 1, y: 0 }}
                        exit={{ opacity: 0, y: -8 }}
                        transition={{
                          duration: 0.22,
                          ease: [0.22, 1, 0.36, 1],
                        }}
                        className="space-y-2"
                      >
                        {detail.rows.map(([label, value]) => (
                          <div key={label} className="flex items-baseline justify-between gap-4 text-xs">
                            <dt className="text-ink-muted">{label}</dt>
                            <dd className="tabular-nums font-medium text-ink">{value}</dd>
                          </div>
                        ))}
                      </motion.dl>
                    ) : (
                      <motion.p
                        key="empty"
                        initial={{ opacity: 0 }}
                        animate={{ opacity: 1 }}
                        exit={{ opacity: 0 }}
                        className="text-xs text-ink-subtle"
                      >
                        Nothing selected. The globe rotates on its own; hover a marker for a summary, click it
                        to pin the detail here.
                      </motion.p>
                    )}
                  </AnimatePresence>
                </div>
              </Card>

              <Card>
                <CardHeader title="Share of book" subtitle="Value booked outside the domestic market" />
                <div className="grid grid-cols-2 gap-2 px-5 pb-5 pt-4">
                  <ShareGauge
                    label="Cross-border"
                    value={data.summary.cross_border_share_pct}
                    caption={`${formatNumber(data.summary.cross_border_transactions)} transactions`}
                    tone="info"
                  />
                  <ShareGauge
                    label="High-risk"
                    value={data.summary.high_risk_share_pct}
                    caption={`${formatNumber(data.summary.high_risk_transactions)} transactions`}
                    tone={data.summary.high_risk_share_pct > 0 ? 'err' : 'ok'}
                  />
                </div>
              </Card>

              <Card>
                <CardHeader title="Ranked by value" subtitle="Every jurisdiction the book touched" />
                <div className="space-y-2.5 px-5 pb-5 pt-4">
                  {data.countries.map((country, index) => {
                    const risk: RiskLevel = country.domestic ? 'domestic' : country.risk_level
                    return (
                      <motion.button
                        key={country.code}
                        type="button"
                        initial={{ opacity: 0, x: -8 }}
                        animate={{ opacity: 1, x: 0 }}
                        transition={{
                          delay: 0.1 + index * 0.05,
                          duration: 0.35,
                          ease: [0.22, 1, 0.36, 1],
                        }}
                        onClick={() =>
                          setSelected(
                            selected === `country:${country.code}` ? null : `country:${country.code}`,
                          )
                        }
                        className={`w-full rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-accent-soft/60 ${
                          selected === `country:${country.code}` ? 'bg-accent-soft/70' : ''
                        }`}
                      >
                        <div className="flex items-baseline justify-between gap-3 text-xs">
                          <span className="flex min-w-0 items-center gap-2">
                            <span
                              className="h-2 w-2 shrink-0 rounded-full"
                              style={{ background: RISK_COLOUR[risk] }}
                            />
                            <span className="truncate font-medium text-ink">{country.name}</span>
                          </span>
                          <span className="shrink-0 tabular-nums text-ink-muted">
                            {formatPercent(country.share_pct)}
                          </span>
                        </div>
                        <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-accent-soft">
                          <motion.div
                            className="h-full rounded-full"
                            style={{ background: RISK_COLOUR[risk] }}
                            initial={{ width: 0 }}
                            animate={{
                              width: `${Math.max(country.share_pct, 0.6)}%`,
                            }}
                            transition={{
                              delay: 0.2 + index * 0.05,
                              duration: 0.7,
                              ease: [0.22, 1, 0.36, 1],
                            }}
                          />
                        </div>
                      </motion.button>
                    )
                  })}
                </div>
              </Card>
            </div>
          </div>

          <Card>
            <CardHeader
              title="Domestic vs cross-border"
              subtitle={`Daily booked value, last ${data.window_days} days`}
            />
            <div className="h-[220px] px-2 pb-3 pt-2">
              {trend.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <AreaChart data={trend} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
                    <defs>
                      <linearGradient id="geo-domestic" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgb(var(--state-ok))" stopOpacity={0.35} />
                        <stop offset="100%" stopColor="rgb(var(--state-ok))" stopOpacity={0} />
                      </linearGradient>
                      <linearGradient id="geo-cross" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="rgb(var(--state-info))" stopOpacity={0.4} />
                        <stop offset="100%" stopColor="rgb(var(--state-info))" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="rgb(var(--line))" strokeDasharray="2 4" vertical={false} />
                    <XAxis dataKey="label" {...chartAxis} minTickGap={28} />
                    <YAxis
                      {...chartAxis}
                      width={44}
                      tickFormatter={(value: number) => formatCompact(value)}
                    />
                    <ReTooltip content={<ChartTooltip format={(value) => money(Number(value), currency)} />} />
                    <Area
                      type="monotone"
                      dataKey="domestic_value"
                      name="Domestic"
                      stroke="rgb(var(--state-ok))"
                      fill="url(#geo-domestic)"
                      strokeWidth={1.6}
                      animationDuration={800}
                    />
                    <Area
                      type="monotone"
                      dataKey="cross_border_value"
                      name="Cross-border"
                      stroke="rgb(var(--state-info))"
                      fill="url(#geo-cross)"
                      strokeWidth={1.6}
                      animationDuration={1000}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              ) : (
                <EmptyState title="No transactions in this window" />
              )}
            </div>
          </Card>

          <Card className="overflow-hidden">
            <div className="px-5 pt-5">
              <Tabs
                value={tab}
                onValueChange={setTab}
                tabs={[
                  {
                    value: 'countries',
                    label: 'Jurisdictions',
                    count: data.countries.length,
                  },
                  {
                    value: 'cities',
                    label: 'Customer locations',
                    count: data.cities.length,
                  },
                  {
                    value: 'corridors',
                    label: 'Corridors',
                    count: data.corridors.length,
                  },
                ]}
              >
                <TabPanel value="countries" className="pb-5">
                  <LocationTable
                    columns={['Jurisdiction', 'Risk', 'Value', 'Share', 'Txns', 'In', 'Out', 'Flagged']}
                    rows={data.countries.map((country) => ({
                      id: `country:${country.code}`,
                      cells: [
                        <span key="n" className="flex items-center gap-2 font-medium text-ink">
                          <MapPinIcon className="h-3.5 w-3.5 text-ink-subtle" />
                          {country.name}
                          <span className="text-2xs text-ink-subtle">{country.code}</span>
                        </span>,
                        <Badge key="r" tone={country.domestic ? 'ok' : RISK_TONE[country.risk_level]}>
                          {RISK_LABEL[country.domestic ? 'domestic' : country.risk_level]}
                        </Badge>,
                        money(country.total_value, currency),
                        formatPercent(country.share_pct),
                        formatNumber(country.transactions),
                        money(country.inbound_value, currency),
                        money(country.outbound_value, currency),
                        formatNumber(country.flagged),
                      ],
                    }))}
                    selected={selected}
                    onSelect={setSelected}
                  />
                </TabPanel>

                <TabPanel value="cities" className="pb-5">
                  <LocationTable
                    columns={['City', 'Customers', 'High risk', 'Value', 'Cross-border', 'Txns', 'Alerts']}
                    rows={data.cities.map((city) => ({
                      id: `city:${city.name}`,
                      cells: [
                        <span key="n" className="flex items-center gap-2 font-medium text-ink">
                          <BuildingOffice2Icon className="h-3.5 w-3.5 text-ink-subtle" />
                          {city.name}
                        </span>,
                        formatNumber(city.customers),
                        formatNumber(city.high_risk_customers),
                        money(city.total_value, currency),
                        money(city.cross_border_value, currency),
                        formatNumber(city.transactions),
                        formatNumber(city.alerts),
                      ],
                    }))}
                    selected={selected}
                    onSelect={setSelected}
                  />
                </TabPanel>

                <TabPanel value="corridors" className="pb-5">
                  <LocationTable
                    columns={['Corridor', 'Risk', 'Value', 'Outbound', 'Inbound', 'Txns', 'Flagged']}
                    rows={data.corridors.map((corridor) => ({
                      id: `country:${corridor.to_code}`,
                      cells: [
                        <span key="n" className="flex items-center gap-2 font-medium text-ink">
                          {corridor.from_city}
                          <ArrowsRightLeftIcon className="h-3.5 w-3.5 text-ink-subtle" />
                          {corridor.to_country}
                        </span>,
                        <Badge key="r" tone={RISK_TONE[corridor.risk_level]}>
                          {RISK_LABEL[corridor.risk_level]}
                        </Badge>,
                        money(corridor.total_value, currency),
                        money(corridor.outbound_value, currency),
                        money(corridor.inbound_value, currency),
                        formatNumber(corridor.transactions),
                        formatNumber(corridor.flagged),
                      ],
                    }))}
                    selected={selected}
                    onSelect={setSelected}
                  />
                </TabPanel>
              </Tabs>
            </div>
          </Card>

          {data.unmapped.length ? (
            <p className="text-2xs text-ink-subtle">
              Not plotted (no reference coordinates): {data.unmapped.join(', ')}
            </p>
          ) : null}
        </>
      )}
    </div>
  )
}

function LocationTable({
  columns,
  rows,
  selected,
  onSelect,
}: {
  columns: string[]
  rows: { id: string; cells: React.ReactNode[] }[]
  selected: string | null
  onSelect: (id: string | null) => void
}) {
  if (!rows.length) return <EmptyState title="Nothing in this window" />
  return (
    <div className="scroll-x">
      <table className="w-full min-w-[46rem] text-xs">
        <thead>
          <tr className="border-b border-line/70 text-left">
            {columns.map((column, index) => (
              <th
                key={column}
                className={`px-3 py-2 metric-label font-medium ${index === 0 ? '' : 'text-right'}`}
              >
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <motion.tr
              key={`${row.id}-${index}`}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                delay: Math.min(index * 0.02, 0.3),
                duration: 0.3,
                ease: [0.22, 1, 0.36, 1],
              }}
              onClick={() => onSelect(selected === row.id ? null : row.id)}
              className={`table-row cursor-pointer ${selected === row.id ? 'bg-accent-soft/70' : ''}`}
            >
              {row.cells.map((cell, cellIndex) => (
                <td
                  key={cellIndex}
                  className={`px-3 py-2 ${cellIndex === 0 ? '' : 'text-right tabular-nums text-ink-muted'}`}
                >
                  {cell}
                </td>
              ))}
            </motion.tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
