'use strict';

const { contextBridge, ipcRenderer } = require('electron');

if (process.isMainFrame) {
  const workspaceListeners = new Set();
  const opaqueId = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(value);
  let receivingWorkspaceRequest = false;
  ipcRenderer.on('avalokita:workspace-request', async (_event, request) => {
    if (!request || typeof request !== 'object' || Array.isArray(request) || Object.keys(request).sort().join(',') !== 'id,requestId'
        || !opaqueId(request.id) || !opaqueId(request.requestId) || receivingWorkspaceRequest) return;
    const listener = workspaceListeners.values().next().value;
    if (!listener) {
      ipcRenderer.send('avalokita:workspace-request-complete', { id: request.id, requestId: request.requestId, handled: false });
      return;
    }
    receivingWorkspaceRequest = true;
    ipcRenderer.send('avalokita:workspace-listener-ready', false);
    try { await listener(request.id); } catch { /* The renderer owns its visible failure message. */ }
    finally {
      ipcRenderer.send('avalokita:workspace-request-complete', { id: request.id, requestId: request.requestId, handled: true });
      receivingWorkspaceRequest = false;
      if (workspaceListeners.size) ipcRenderer.send('avalokita:workspace-listener-ready', true);
    }
  });
  contextBridge.exposeInMainWorld('avalokitaDesktop', Object.freeze({
    listWorkspaces: () => ipcRenderer.invoke('avalokita:workspaces:list'),
    chooseWorkspace: () => ipcRenderer.invoke('avalokita:workspaces:choose'),
    activateWorkspace: (id, context) => ipcRenderer.invoke('avalokita:workspaces:activate', id, context),
    onWorkspaceActivationRequested: listener => {
      if (typeof listener !== 'function') throw new TypeError('Workspace listener must be a function');
      workspaceListeners.add(listener);
      if (workspaceListeners.size === 1 && !receivingWorkspaceRequest) ipcRenderer.send('avalokita:workspace-listener-ready', true);
      let subscribed = true;
      return () => {
        if (!subscribed) return;
        subscribed = false;
        workspaceListeners.delete(listener);
        if (!workspaceListeners.size) ipcRenderer.send('avalokita:workspace-listener-ready', false);
      };
    },
  }));
}
