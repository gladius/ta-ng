import { Route, Routes } from 'react-router-dom'
import Shell from './Shell'
import Home from './pages/Home'
import Batches from './pages/Batches'
import Batch from './pages/Batch'
import Review from './pages/Review'
import { Agents, Sources, Workspaces } from './pages/Browse'
import { Analyze, Audit, Build } from './pages/Progress'
import Select from './pages/Select'
import Report from './pages/Report'
import Profile from './pages/Profile'
import { Migrations, Models } from './pages/Catalog'
import { CacheOverview, CacheSite, CacheUsecase } from './pages/Cache'
import { CallSite, CallSiteUsecase, CallSiteUsecases } from './pages/CallSites'
import { Page } from './ui'

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Home />} />
        <Route path="validations" element={<Batches />} />
        <Route path="validations/:jobId" element={<Batch />} />
        <Route path="validations/:jobId/requests/*" element={<Review />} />
        <Route path="sources" element={<Sources />} />
        <Route path="sources/:source" element={<Workspaces />} />
        <Route path="sources/:source/:ws" element={<Agents />} />
        <Route path="agents/:source/:ws/:project/build" element={<Build />} />
        <Route path="agents/:source/:ws/:project/select" element={<Select />} />
        <Route path="agents/:source/:ws/:project/audit" element={<Audit />} />
        <Route path="agents/:source/:ws/:project/report" element={<Report />} />
        <Route path="agents/:source/:ws/:project/profile" element={<Profile />} />
        <Route path="agents/:source/:ws/:project/analyze" element={<Analyze />} />
        <Route path="usecases" element={<CallSiteUsecases />} />
        <Route path="usecases/:usecase" element={<CallSiteUsecase />} />
        <Route path="usecases/:usecase/callsites/:site" element={<CallSite />} />
        <Route path="cache" element={<CacheOverview />} />
        <Route path="cache/usecases/:usecase" element={<CacheUsecase />} />
        <Route path="cache/sites/:site" element={<CacheSite />} />
        <Route path="models" element={<Models />} />
        <Route path="models/migrations/:provider" element={<Migrations />} />
        <Route path="*" element={<Page crumbs={[]}><p className="empty">No such page.</p></Page>} />
      </Route>
    </Routes>
  )
}
