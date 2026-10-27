import type { IncomingMessage, ServerResponse } from 'node:http';
import { createCluster, deleteCluster, getResolvedClusters, toClusterDto, updateCluster, updateClusterLoginKubeconfigs } from '../clusters/store.ts';
import { listNamespaces } from '../kubernetes/namespaces.ts';
import { listDeployments } from '../kubernetes/deployments.ts';
import { listPods } from '../kubernetes/pods.ts';
import { listResourceQuotas } from '../kubernetes/quotas.ts';
import { listConfigMaps, replaceConfigMap } from '../kubernetes/configmaps.ts';
import { testCluster, testClusterInput } from '../kubernetes/test-cluster.ts';
import {
  deleteDeployment,
  deletePod,
  execPod,
  getDeploymentDetail,
  getDeploymentEvents,
  getPodDetail,
  getPodEvents,
  getPodLogs,
  getPodMetrics,
  replaceDeploymentResource,
  replacePodResource,
  restartDeployment,
  setDeploymentReplicas,
  updateDeploymentResources
} from '../kubernetes/resources.ts';
import type { ClusterInputDto, DeploymentResourcesInputDto, ExecInputDto } from '../types/api.ts';
import { getClusterHealth } from '../kubernetes/cluster-health.ts';
import { authenticateKubectlLoginClusters, authenticateKubectlLoginInput } from '../kubernetes/kubectl-login-cli.ts';
import { logError, logInfo, requestAction } from '../logging/logger.ts';
import { stopClusterProxy } from '../kubernetes/kubectl-proxy.ts';

function sendJson(response: ServerResponse, status: number, data: unknown): void {
  const body = JSON.stringify(data);
  response.statusCode = status;
  response.setHeader('Content-Type', 'application/json; charset=utf-8');
  response.end(body);
}

function sendText(response: ServerResponse, status: number, data: string): void {
  response.statusCode = status;
  response.setHeader('Content-Type', 'text/plain; charset=utf-8');
  response.end(data);
}

export function safeErrorMessage(error: unknown): string {
  const raw = error instanceof Error ? error.message : typeof error === 'string' ? error : 'Unknown server error';
  const httpCode = raw.match(/HTTP-Code:\s*(\d{3})/i)?.[1] ?? raw.match(/\bHTTP\s+(\d{3})\b/i)?.[1];
  const containsHtml = /<(?:!doctype|html|head|body|meta|script|style)\b/i.test(raw);
  if (containsHtml) {
    return httpCode
      ? `Kubernetes API вернул HTTP ${httpCode}. Проверьте адрес API-сервера и параметры подключения.`
      : 'Kubernetes API вернул HTML вместо данных. Проверьте адрес API-сервера и параметры подключения.';
  }

  const compact = raw.replace(/\s+/g, ' ').trim();
  return compact.length > 500 ? `${compact.slice(0, 497)}…` : compact;
}

function sendError(response: ServerResponse, error: unknown, status = 500): void {
  const message = safeErrorMessage(error);
  // Do not dump arbitrary Kubernetes client error objects: they may carry
  // request metadata. Credentials are deliberately never written to logs.
  logError('API request failed', message, { status });
  sendJson(response, status, { error: message });
}

async function readJson<T>(request: IncomingMessage): Promise<T> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > 5_000_000) throw new Error('Request body is too large.');
    chunks.push(buffer);
  }
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) throw new Error('JSON request body is required.');
  return JSON.parse(text) as T;
}

function clusterId(url: URL): string {
  const id = url.searchParams.get('clusterId');
  if (!id || !/^[0-9a-f-]{36}$/i.test(id)) throw new Error('A valid clusterId is required.');
  return id;
}

function requiredNamespace(url: URL): string {
  const namespace = url.searchParams.get('namespace');
  if (!namespace) throw new Error('namespace is required');
  if (!/^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$/.test(namespace)) throw new Error('namespace is invalid.');
  return namespace;
}

function pathPart(value: string): string {
  const decoded = decodeURIComponent(value);
  if (decoded.length > 253 || !/^[a-z0-9](?:[-.a-z0-9]*[a-z0-9])?$/.test(decoded)) throw new Error('Kubernetes resource name is invalid.');
  return decoded;
}

function validateResourcePayload(value: unknown, depth = 0): void {
  if (depth > 40) throw new Error('Resource structure is too deeply nested.');
  if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return;
  if (Array.isArray(value)) { value.forEach((item) => validateResourcePayload(item, depth + 1)); return; }
  if (typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) throw new Error('Resource must contain JSON data only.');
  for (const [key, item] of Object.entries(value)) {
    if (key === '__proto__' || key === 'prototype' || key === 'constructor') throw new Error('Resource contains a forbidden key.');
    validateResourcePayload(item, depth + 1);
  }
}

export async function handleApiRequest(
  request: IncomingMessage,
  response: ServerResponse,
  url: URL
): Promise<boolean> {
  if (!url.pathname.startsWith('/api/')) return false;

  try {
    const action = requestAction(request.method, url.pathname);
    if (action) logInfo(action, { clusterId: url.searchParams.get('clusterId'), namespace: url.searchParams.get('namespace') });
    if (request.method === 'GET' && url.pathname === '/api/health') {
      const clusters = getResolvedClusters();
      sendJson(response, 200, {
        ok: true,
        clusters: clusters.length,
        enabledClusters: clusters.filter((cluster) => cluster.enabled).length
      });
      return true;
    }

    if (request.method === 'GET' && url.pathname === '/api/clusters') {
      sendJson(response, 200, getResolvedClusters().map(toClusterDto));
      return true;
    }

    if (request.method === 'POST' && url.pathname === '/api/clusters') {
      try {
        const input = await authenticateKubectlLoginInput(await readJson<ClusterInputDto>(request));
        sendJson(response, 201, createCluster(input));
      } catch (error) {
        sendError(response, error, 400);
      }
      return true;
    }
    if (request.method === 'POST' && url.pathname === '/api/clusters/test-input') {
      try {
        const payload = await readJson<{ input: ClusterInputDto; existingId?: string }>(request);
        sendJson(response, 200, await testClusterInput(payload.input, payload.existingId));
      } catch (error) {
        sendError(response, error, 400);
      }
      return true;
    }
    if (request.method === 'POST' && url.pathname === '/api/clusters/login') {
      try {
        const payload = await readJson<{ clusterIds?: unknown; password?: unknown }>(request);
        const clusterIds = payload.clusterIds;
        const password = payload.password;
        if (!Array.isArray(clusterIds) || clusterIds.some((id) => typeof id !== 'string') || typeof password !== 'string') throw new Error('Login request is invalid.');
        const kubeconfigs = await authenticateKubectlLoginClusters(clusterIds as string[], password);
        sendJson(response, 200, updateClusterLoginKubeconfigs(kubeconfigs));
      } catch (error) {
        sendError(response, error, 400);
      }
      return true;
    }

    const clusterRoute = url.pathname.match(/^\/api\/clusters\/([^/]+)$/);
    if (clusterRoute && request.method === 'PUT') {
      try {
        const id = pathPart(clusterRoute[1]);
        const input = await authenticateKubectlLoginInput(await readJson<ClusterInputDto>(request), id);
        sendJson(response, 200, updateCluster(id, input));
      } catch (error) {
        sendError(response, error, 400);
      }
      return true;
    }
    if (clusterRoute && request.method === 'DELETE') {
      try {
        const id = pathPart(clusterRoute[1]);
        stopClusterProxy(id, 'cluster removed');
        deleteCluster(id);
        response.statusCode = 204;
        response.end();
      } catch (error) {
        sendError(response, error, 400);
      }
      return true;
    }

    const testRoute = url.pathname.match(/^\/api\/clusters\/([^/]+)\/test$/);
    if (testRoute && request.method === 'POST') {
      try {
        sendJson(response, 200, await testCluster(pathPart(testRoute[1])));
      } catch (error) {
        sendError(response, error, 400);
      }
      return true;
    }

    if (request.method === 'GET' && url.pathname === '/api/namespaces') {
      sendJson(response, 200, await listNamespaces(clusterId(url)));
      return true;
    }
    if (request.method === 'GET' && url.pathname === '/api/cluster-health') {
      sendJson(response, 200, await getClusterHealth(clusterId(url)));
      return true;
    }
    if (request.method === 'GET' && url.pathname === '/api/deployments') {
      sendJson(response, 200, await listDeployments(clusterId(url), requiredNamespace(url)));
      return true;
    }
    if (request.method === 'GET' && url.pathname === '/api/pods') {
      sendJson(response, 200, await listPods(clusterId(url), requiredNamespace(url)));
      return true;
    }
    if (request.method === 'GET' && url.pathname === '/api/resource-quotas') {
      sendJson(response, 200, await listResourceQuotas(clusterId(url), requiredNamespace(url)));
      return true;
    }
    if (request.method === 'GET' && url.pathname === '/api/configmaps') {
      sendJson(response, 200, await listConfigMaps(clusterId(url), requiredNamespace(url)));
      return true;
    }

    const configMapRoute = url.pathname.match(/^\/api\/configmaps\/([^/]+)$/);
    if (configMapRoute && request.method === 'PUT') {
      const payload = await readJson<{ resource: unknown }>(request);
      validateResourcePayload(payload.resource);
      sendJson(response, 200, await replaceConfigMap(clusterId(url), requiredNamespace(url), pathPart(configMapRoute[1]), payload.resource));
      return true;
    }

    const deploymentRoute = url.pathname.match(/^\/api\/deployments\/([^/]+)$/);
    if (deploymentRoute) {
      const name = pathPart(deploymentRoute[1]);
      const namespace = requiredNamespace(url);
      const cluster = clusterId(url);
      if (request.method === 'GET') {
        sendJson(response, 200, await getDeploymentDetail(cluster, namespace, name));
        return true;
      }
      if (request.method === 'DELETE') {
        await deleteDeployment(cluster, namespace, name);
        response.statusCode = 204;
        response.end();
        return true;
      }
      if (request.method === 'PUT') {
        const payload = await readJson<{ resource: unknown }>(request);
        validateResourcePayload(payload.resource);
        sendJson(response, 200, await replaceDeploymentResource(cluster, namespace, name, payload.resource));
        return true;
      }
    }

    const deploymentReplicas = url.pathname.match(/^\/api\/deployments\/([^/]+)\/replicas$/);
    if (deploymentReplicas && request.method === 'PUT') {
      const payload = await readJson<{ replicas: number }>(request);
      sendJson(response, 200, await setDeploymentReplicas(clusterId(url), requiredNamespace(url), pathPart(deploymentReplicas[1]), payload.replicas));
      return true;
    }

    const deploymentRestart = url.pathname.match(/^\/api\/deployments\/([^/]+)\/restart$/);
    if (deploymentRestart && request.method === 'POST') {
      sendJson(response, 200, await restartDeployment(clusterId(url), requiredNamespace(url), pathPart(deploymentRestart[1])));
      return true;
    }

    const deploymentResources = url.pathname.match(/^\/api\/deployments\/([^/]+)\/resources$/);
    if (deploymentResources && request.method === 'PUT') {
      const payload = await readJson<DeploymentResourcesInputDto>(request);
      sendJson(response, 200, await updateDeploymentResources(clusterId(url), requiredNamespace(url), pathPart(deploymentResources[1]), payload));
      return true;
    }

    const deploymentEvents = url.pathname.match(/^\/api\/deployments\/([^/]+)\/events$/);
    if (deploymentEvents && request.method === 'GET') {
      sendJson(response, 200, await getDeploymentEvents(clusterId(url), requiredNamespace(url), pathPart(deploymentEvents[1])));
      return true;
    }

    const podRoute = url.pathname.match(/^\/api\/pods\/([^/]+)$/);
    if (podRoute) {
      const name = pathPart(podRoute[1]);
      const namespace = requiredNamespace(url);
      const cluster = clusterId(url);
      if (request.method === 'GET') {
        sendJson(response, 200, await getPodDetail(cluster, namespace, name));
        return true;
      }
      if (request.method === 'DELETE') {
        await deletePod(cluster, namespace, name);
        response.statusCode = 204;
        response.end();
        return true;
      }
      if (request.method === 'PUT') {
        const payload = await readJson<{ resource: unknown }>(request);
        validateResourcePayload(payload.resource);
        sendJson(response, 200, await replacePodResource(cluster, namespace, name, payload.resource));
        return true;
      }
    }

    const podLogs = url.pathname.match(/^\/api\/pods\/([^/]+)\/logs$/);
    if (podLogs && request.method === 'GET') {
      const tail = Number(url.searchParams.get('tailLines') ?? 500);
      const lines = Number.isFinite(tail) ? Math.min(10_000, Math.max(1, Math.trunc(tail))) : 500;
      sendText(response, 200, await getPodLogs(
        clusterId(url),
        requiredNamespace(url),
        pathPart(podLogs[1]),
        url.searchParams.get('container') || undefined,
        lines,
        url.searchParams.get('previous') === '1'
      ));
      return true;
    }

    const podExec = url.pathname.match(/^\/api\/pods\/([^/]+)\/exec$/);
    if (podExec && request.method === 'POST') {
      const payload = await readJson<ExecInputDto>(request);
      sendJson(response, 200, await execPod(clusterId(url), requiredNamespace(url), pathPart(podExec[1]), payload));
      return true;
    }

    const podMetrics = url.pathname.match(/^\/api\/pods\/([^/]+)\/metrics$/);
    if (podMetrics && request.method === 'GET') {
      sendJson(response, 200, await getPodMetrics(clusterId(url), requiredNamespace(url), pathPart(podMetrics[1])));
      return true;
    }

    const podEvents = url.pathname.match(/^\/api\/pods\/([^/]+)\/events$/);
    if (podEvents && request.method === 'GET') {
      sendJson(response, 200, await getPodEvents(clusterId(url), requiredNamespace(url), pathPart(podEvents[1])));
      return true;
    }

    sendJson(response, 404, { error: 'Not found' });
    return true;
  } catch (error) {
    sendError(response, error);
    return true;
  }
}
