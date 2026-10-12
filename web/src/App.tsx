import { Route, Routes } from 'react-router-dom'
import Shell from './Shell'
import Home from './pages/Home'
import Batches from './pages/Batches'
import Batch from './pages/Batch'
import Review from './pages/Review'
import { Migrations, Models } from './pages/Catalog'
import { CacheOverview, CacheSite, CacheUsecase } from './pages/Cache'
import { CallSite, CallSiteUsecase, CallSiteUsecases } from './pages/CallSites'
import { ModelAuditDetail, ModelAudits } from './pages/ModelAudits'
import How from './pages/How'
import { Page } from './ui'

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Home />} />
        <Route path="validations" element={<Batches />} />
        <Route path="validations/:jobId" element={<Batch />} />
        <Route path="validations/:jobId/requests/*" element={<Review />} />
        <Route path="audit/models" element={<ModelAudits />} />
        <Route path="audit/models/how" element={<How />} />
        <Route path="audit/models/:auditId" element={<ModelAuditDetail />} />
        <Route path="usecases" element={<CallSiteUsecases />} />
        <Route path="usecases/:usecase" element={<CallSiteUsecase />} />
        <Route path="usecases/:usecase/callsites/:site" element={<CallSite />} />
        <Route path="audit/cache" element={<CacheOverview />} />
        <Route path="audit/cache/usecases/:usecase" element={<CacheUsecase />} />
        <Route path="audit/cache/sites/:site" element={<CacheSite />} />
        <Route path="models" element={<Models />} />
        <Route path="models/migrations/:provider" element={<Migrations />} />
        <Route path="*" element={<Page crumbs={[]}><p className="empty">No such page.</p></Page>} />
      </Route>
    </Routes>
  )
}
