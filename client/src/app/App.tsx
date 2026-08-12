import { useState } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from './queryClient';
import { Matrix } from '../components/matrix/Matrix';
import { QuotasPage } from '../components/quotas/QuotasPage';
import { ConfigMapsPage } from '../components/configmaps/ConfigMapsPage';
import { ClusterPanel } from '../components/clusters/ClusterPanel';
import { DeploymentPage } from '../components/deployment/DeploymentPage';
import { PodPanel } from '../components/pod/PodPanel';
import { useClusters } from '../hooks/useKubernetes';
import type { PodRef, WorkloadRef } from '../types/kubernetes';
import './App.css';

type View = 'pods' | 'quotas' | 'configmaps';

function Shell() {
  const [view, setView] = useState<View>('pods');
  const [clustersOpen, setClustersOpen] = useState(false);
  const [deployment, setDeployment] = useState<WorkloadRef | null>(null);
  const [pod, setPod] = useState<PodRef | null>(null);
  const clustersQuery = useClusters();
  const enabled = (clustersQuery.data ?? []).filter((cluster) => cluster.enabled).length;

  const switchView = (next: View) => {
    setDeployment(null);
    setView(next);
  };

  return (
    <div className="app">
      <header className="top-nav">
        <button className="brand" onClick={() => switchView('pods')} aria-label="Quaber home">
          <img src="/logo.png" alt="Quaber" />
          <span>Quaber</span>
        </button>


        <nav className="nav-tabs" aria-label="Основная навигация">
          <button className={`nav-tab ${view === 'pods' && !deployment ? 'active' : ''}`} onClick={() => switchView('pods')}>Список подов</button>
          <button className={`nav-tab ${view === 'quotas' ? 'active' : ''}`} onClick={() => switchView('quotas')}>Квоты ресурсов</button>
          <button className={`nav-tab ${view === 'configmaps' ? 'active' : ''}`} onClick={() => switchView('configmaps')}>ConfigMaps</button>
        </nav>

        <button className="user-menu" onClick={() => setClustersOpen(true)}>
          <span className="user-menu-name">Локальный пользователь</span>
          <span className="user-menu-subtitle">Кластеры: {enabled} · Настройки</span>
        </button>
      </header>

      <main className="content">
        {view === 'pods' && !deployment && <Matrix onOpenDeployment={setDeployment} onOpenPod={setPod} />}
        {view === 'pods' && deployment && <DeploymentPage workload={deployment} onOpenPod={setPod} onDeleted={() => setDeployment(null)} />}
        {view === 'quotas' && <QuotasPage />}
        {view === 'configmaps' && <ConfigMapsPage />}
      </main>

      <ClusterPanel open={clustersOpen} onClose={() => setClustersOpen(false)} />
      <PodPanel pod={pod} onClose={() => setPod(null)} />
    </div>
  );
}

export function App() {
  return <QueryClientProvider client={queryClient}><Shell /></QueryClientProvider>;
}
