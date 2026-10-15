import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import {
  ArrowUpRight, BookOpen, Braces, Clock, Database, Fingerprint, LayoutDashboard, Menu, Route as RouteIcon, TrendingDown, X,
  type LucideIcon,
} from 'lucide-react'

type EntryProps = { icon: LucideIcon; label: string; to?: string; href?: string; end?: boolean; active?: boolean; later?: boolean }

/** One navigation entry: an icon beside its label. */
function Entry({ icon: Icon, label, to, href, end, active, later }: EntryProps) {
  const inner = <><Icon size={18} strokeWidth={1.8} aria-hidden="true" /><span>{label}</span>{later && <span className="chip">later</span>}</>
  if (later) return <span className="item off">{inner}</span>
  if (href) {                                    // outside the app (the API reference): its own tab
    return <a className="item" href={href} target="_blank" rel="noopener">{inner}<ArrowUpRight className="ext" size={14} aria-label="opens in a new tab" /></a>
  }
  if (active !== undefined) {
    return <Link className={'item' + (active ? ' active' : '')} to={to!} aria-current={active ? 'page' : undefined}>{inner}</Link>
  }
  return <NavLink className="item" to={to!} end={end}>{inner}</NavLink>
}

/** The suite's one navigation: fixed while the page scrolls; on a phone, a menu behind a top bar. Tier downgrade and
    Open-weight are the same agent audit in its two modes. */
export default function Shell() {
  const { pathname, search } = useLocation()
  const [menu, setMenu] = useState(false)
  useEffect(() => { setMenu(false) }, [pathname, search])               // a phone's menu closes once you go somewhere
  const brand = <div className="brand"><span className="mark" aria-hidden="true"><i /><b /></span><span>Token Auditor</span></div>

  return (
    <div className="shell">
      <header className="phone-bar">
        {brand}
        <button type="button" className="icon-btn" aria-label={menu ? 'Close the menu' : 'Open the menu'} aria-expanded={menu} onClick={() => setMenu(!menu)}>
          {menu ? <X size={20} aria-hidden="true" /> : <Menu size={20} aria-hidden="true" />}
        </button>
      </header>
      <nav className={'nav' + (menu ? ' open' : '')} aria-label="Suite">
        {brand}
        <div className="group">
          <Entry icon={LayoutDashboard} label="Overview" to="/" end />
        </div>
        <div className="group">
          <span className="group-name">Agentic workloads</span>
          <Entry icon={Fingerprint} label="Usecases" to="/usecases" />
          <Entry icon={TrendingDown} label="Model audits" to="/audit/models" />
          <Entry icon={Database} label="Cache audit" to="/audit/cache" />
        </div>
        <div className="group">
          <span className="group-name">Router</span>
          <Entry icon={RouteIcon} label="Pins and rollout" later />
        </div>
        <div className="group foot">
          <Entry icon={BookOpen} label="Model catalog" to="/models" />
          <Entry icon={Clock} label="Schedules" to="/schedules" />
          <Entry icon={Braces} label="API reference" href="/docs" />
        </div>
      </nav>
      <Outlet />
    </div>
  )
}
