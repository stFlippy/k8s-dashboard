import { useCallback, useEffect, useState } from 'react';
import { QueryClientProvider } from '@tanstack/react-query';
import { queryClient } from './queryClient';
import { Matrix } from '../components/matrix/Matrix';
import { QuotasPage } from '../components/quotas/QuotasPage';
import { ConfigMapsPage } from '../components/configmaps/ConfigMapsPage';
import { ClusterPanel } from '../components/clusters/ClusterPanel';
import { ClusterLoginModal } from '../components/clusters/ClusterLoginModal';
import { DeploymentPage } from '../components/deployment/DeploymentPage';
import { PodPanel } from '../components/pod/PodPanel';
import { ClusterHealthPage } from '../components/health/ClusterHealthPage';
import { useClusters } from '../hooks/useKubernetes';
import type { PodRef, WorkloadRef } from '../types/kubernetes';
import './App.css';
import { AuthScreen } from '../components/auth/AuthScreen';
import { getCurrentUser, getPreferences, logout, savePreferences } from '../api/auth';
import { clusterAuthorizationEvent, setCsrfToken } from '../api/client';
import type { UserAccount } from '../types/kubernetes';

type View = 'pods' | 'quotas' | 'configmaps' | 'health';

function NotFoundPage() {
  return <main className="not-found"><div><strong>404</strong><h1>Страница не найдена</h1><p>Такого адреса в Quaber нет.</p><button onClick={() => window.location.assign('/')}>На главную</button></div></main>;
}

function Shell({ user, onLoggedOut }: { user: UserAccount; onLoggedOut: () => void }) {
  const [view, setView] = useState<View>('pods');
  const [clustersOpen, setClustersOpen] = useState(false);
  const [deployment, setDeployment] = useState<WorkloadRef | null>(null);
  const [pod, setPod] = useState<PodRef | null>(null);
  const [unauthorizedClusterIds, setUnauthorizedClusterIds] = useState<string[]>([]);
  const [dismissedLoginSignature, setDismissedLoginSignature] = useState('');
  const clustersQuery = useClusters();
  const enabled = (clustersQuery.data ?? []).filter((cluster) => cluster.enabled).length;
  const loginClusters = (clustersQuery.data ?? []).filter((cluster) => cluster.enabled && cluster.authType === 'kubectl-login' && (cluster.loginRequired || unauthorizedClusterIds.includes(cluster.id)));
  const loginSignature = loginClusters.map((cluster) => cluster.id).sort().join(',');
  const markClustersUnauthorized = useCallback((ids: string[]) => {
    setUnauthorizedClusterIds((current) => {
      const next = [...new Set([...current, ...ids])];
      return next.length === current.length ? current : next;
    });
  }, []);
  useEffect(() => {
    const requireAuthorization = (event: Event) => {
      const clusterId = (event as CustomEvent<string>).detail;
      if (clusterId) markClustersUnauthorized([clusterId]);
    };
    window.addEventListener(clusterAuthorizationEvent, requireAuthorization);
    return () => window.removeEventListener(clusterAuthorizationEvent, requireAuthorization);
  }, [markClustersUnauthorized]);

  const switchView = (next: View) => {
    setDeployment(null);
    setView(next);
  };

  const openHome = () => {
    setPod(null);
    setDeployment(null);
    setClustersOpen(false);
    setView('pods');
  };

  return (
    <div className="app">
      <header className="top-nav">
        <button className="brand" onClick={openHome} aria-label="Открыть главную страницу Quaber">
          <img src="/logo.png" alt="Quaber" />
          <span>Quaber</span>
        </button>


        <nav className="nav-tabs" aria-label="Основная навигация">
          <button className={`nav-tab ${view === 'pods' ? 'active' : ''}`} onClick={() => switchView('pods')}>Pod list</button>
          <button className={`nav-tab ${view === 'quotas' ? 'active' : ''}`} onClick={() => switchView('quotas')}>Квоты ресурсов</button>
          <button className={`nav-tab ${view === 'configmaps' ? 'active' : ''}`} onClick={() => switchView('configmaps')}>ConfigMaps</button>
          <button className={`nav-tab ${view === 'health' ? 'active' : ''}`} onClick={() => switchView('health')}>Cluster Health</button>
        </nav>

        <button className="user-menu" onClick={() => setClustersOpen(true)}>
          <span className="user-menu-name">{user.username}</span>
          <span className="user-menu-subtitle">Кластеры: {enabled} · Настройки</span>
        </button>
      </header>

      <main className="content">
        {view === 'pods' && <Matrix onOpenDeployment={setDeployment} onOpenPod={setPod} onClustersUnauthorized={markClustersUnauthorized} />}
        {view === 'pods' && deployment && <DeploymentPage workload={deployment} onOpenPod={setPod} onClose={() => setDeployment(null)} />}
        {view === 'quotas' && <QuotasPage />}
        {view === 'configmaps' && <ConfigMapsPage />}
        {view === 'health' && <ClusterHealthPage />}
      </main>

      <ClusterPanel open={clustersOpen} onClose={() => setClustersOpen(false)} onLogout={() => { void logout().finally(() => { setCsrfToken(''); onLoggedOut(); }); }} />
      <PodPanel pod={pod} onClose={() => setPod(null)} />
      {loginClusters.length > 0 && loginSignature !== dismissedLoginSignature && <ClusterLoginModal
        clusters={loginClusters}
        onClose={() => setDismissedLoginSignature(loginSignature)}
        onAuthenticated={(ids) => { setUnauthorizedClusterIds((current) => current.filter((id) => !ids.includes(id))); setDismissedLoginSignature(''); }}
      />}
    </div>
  );
}

export function App() {
  if (!['/', '/index.html'].includes(window.location.pathname)) return <NotFoundPage />;
  const [user, setUser] = useState<UserAccount | null | undefined>(undefined);
  const [preferencesReady, setPreferencesReady] = useState(false);
  useEffect(() => { void getCurrentUser().then(setUser).catch(() => setUser(null)); }, []);
  useEffect(() => {
    if (!user || user.mustChangePassword) { setPreferencesReady(false); return; }
    let active = true;
    void getPreferences().then((preferences) => { if (!active) return; Object.keys(window.localStorage).filter((key) => key.startsWith('quaber.')).forEach((key) => window.localStorage.removeItem(key)); Object.entries(preferences).forEach(([key,value]) => { if (key.startsWith('quaber.') && typeof value === 'string') window.localStorage.setItem(key,value); }); setPreferencesReady(true); }).catch(() => setPreferencesReady(true));
    return () => { active=false; };
  }, [user?.id, user?.mustChangePassword]);
  useEffect(() => {
    if (!user || user.mustChangePassword || !preferencesReady) return;
    const persist=()=>{const values:Record<string,string>={};for(let index=0;index<window.localStorage.length;index++){const key=window.localStorage.key(index);if(key?.startsWith('quaber.'))values[key]=window.localStorage.getItem(key)??'';}void savePreferences(values).catch(()=>undefined);};
    const timer=window.setInterval(persist,3000);window.addEventListener('beforeunload',persist);return()=>{window.clearInterval(timer);window.removeEventListener('beforeunload',persist);persist();};
  }, [preferencesReady,user?.id,user?.mustChangePassword]);
  if (user === undefined || (user && !user.mustChangePassword && !preferencesReady)) return <div className="auth-loading">Загрузка настроек пользователя…</div>;
  if (!user || user.mustChangePassword) return <AuthScreen user={user} onAuthenticated={setUser} />;
  return <QueryClientProvider client={queryClient}><Shell user={user} onLoggedOut={() => { queryClient.clear(); setUser(null); }} /></QueryClientProvider>;
}
