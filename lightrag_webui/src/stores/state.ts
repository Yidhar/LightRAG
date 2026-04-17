import { create } from 'zustand'
import { createSelectors } from '@/lib/utils'
import { checkHealth, LightragStatus } from '@/api/lightrag'
import { useSettingsStore } from './settings'
import { healthCheckInterval } from '@/lib/constants'
import { type MembershipClaim, normalizeMembershipClaims } from '@/lib/permissions'

interface BackendState {
  health: boolean
  message: string | null
  messageTitle: string | null
  status: LightragStatus | null
  lastCheckTime: number
  pipelineBusy: boolean
  healthCheckIntervalId: ReturnType<typeof setInterval> | null
  healthCheckFunction: (() => void) | null
  healthCheckIntervalValue: number

  check: () => Promise<boolean>
  clear: () => void
  setErrorMessage: (message: string, messageTitle: string) => void
  setPipelineBusy: (busy: boolean) => void
  setHealthCheckFunction: (fn: () => void) => void
  resetHealthCheckTimer: () => void
  resetHealthCheckTimerDelayed: (delayMs: number) => void
  clearHealthCheckTimer: () => void
}

interface AuthState {
  isAuthenticated: boolean;
  coreVersion: string | null;
  apiVersion: string | null;
  username: string | null; // login username
  role: string | null; // raw token role
  memberships: MembershipClaim[]; // JWT membership claims
  webuiTitle: string | null; // Custom title
  webuiDescription: string | null; // Title description
  lastTokenRenewal: string | null; // Human-readable local time of last token renewal (for debugging and monitoring)
  tokenExpiresAt: number | null; // Token expiration timestamp (extracted from JWT)

  login: (token: string, coreVersion?: string | null, apiVersion?: string | null, webuiTitle?: string | null, webuiDescription?: string | null) => void;
  logout: () => void;
  setVersion: (coreVersion: string | null, apiVersion: string | null) => void;
  setCustomTitle: (webuiTitle: string | null, webuiDescription: string | null) => void;
  setTokenRenewal: (renewalTime: number, expiresAt: number) => void; // Track token renewal
  // Locally grant a membership claim without rotating the JWT.
  // Used after actions that create a new membership (e.g. POST /workspaces
  // auto-grants owner on the new workspace) — the backend has the real
  // claim in its DB, we just patch the frontend's JWT-derived copy so
  // UI gating unlocks immediately instead of on next login.
  grantMembershipClaim: (claim: MembershipClaim) => void;
}

const useBackendStateStoreBase = create<BackendState>()((set, get) => ({
  health: true,
  message: null,
  messageTitle: null,
  lastCheckTime: Date.now(),
  status: null,
  pipelineBusy: false,
  healthCheckIntervalId: null,
  healthCheckFunction: null,
  healthCheckIntervalValue: healthCheckInterval * 1000, // Use constant from lib/constants

  check: async () => {
    const health = await checkHealth()
    if (health.status === 'healthy') {
      // Update version information if health check returns it
      if (health.core_version || health.api_version) {
        useAuthStore.getState().setVersion(
          health.core_version || null,
          health.api_version || null
        );
      }

      // Update custom title information if health check returns it
      if ('webui_title' in health || 'webui_description' in health) {
        useAuthStore.getState().setCustomTitle(
          'webui_title' in health ? (health.webui_title ?? null) : null,
          'webui_description' in health ? (health.webui_description ?? null) : null
        );
      }

      // Extract and store backend max graph nodes limit
      if (health.configuration?.max_graph_nodes) {
        const maxNodes = parseInt(health.configuration.max_graph_nodes, 10)
        if (!isNaN(maxNodes) && maxNodes > 0) {
          const currentBackendMaxNodes = useSettingsStore.getState().backendMaxGraphNodes

          // Only update if the backend limit has actually changed
          if (currentBackendMaxNodes !== maxNodes) {
            useSettingsStore.getState().setBackendMaxGraphNodes(maxNodes)

            // Auto-adjust current graphMaxNodes if it exceeds the new backend limit
            const currentMaxNodes = useSettingsStore.getState().graphMaxNodes
            if (currentMaxNodes > maxNodes) {
              useSettingsStore.getState().setGraphMaxNodes(maxNodes, true)
            }
          }
        }
      }

      set({
        health: true,
        message: null,
        messageTitle: null,
        lastCheckTime: Date.now(),
        status: health,
        pipelineBusy: health.pipeline_busy
      })
      return true
    }
    set({
      health: false,
      message: health.message,
      messageTitle: 'Backend Health Check Error!',
      lastCheckTime: Date.now(),
      status: null
    })
    return false
  },

  clear: () => {
    set({ health: true, message: null, messageTitle: null })
  },

  setErrorMessage: (message: string, messageTitle: string) => {
    set({ health: false, message, messageTitle })
  },

  setPipelineBusy: (busy: boolean) => {
    set({ pipelineBusy: busy })
  },

  setHealthCheckFunction: (fn: () => void) => {
    set({ healthCheckFunction: fn })
  },

  resetHealthCheckTimer: () => {
    const { healthCheckIntervalId, healthCheckFunction, healthCheckIntervalValue } = get()
    if (healthCheckIntervalId) {
      clearInterval(healthCheckIntervalId)
    }
    if (healthCheckFunction) {
      healthCheckFunction() // run health check immediately
      const newIntervalId = setInterval(healthCheckFunction, healthCheckIntervalValue)
      set({ healthCheckIntervalId: newIntervalId })
    }
  },

  resetHealthCheckTimerDelayed: (delayMs: number) => {
    setTimeout(() => {
      get().resetHealthCheckTimer()
    }, delayMs)
  },

  clearHealthCheckTimer: () => {
    const { healthCheckIntervalId } = get()
    if (healthCheckIntervalId) {
      clearInterval(healthCheckIntervalId)
      set({ healthCheckIntervalId: null })
    }
  }
}))

const useBackendState = createSelectors(useBackendStateStoreBase)

export { useBackendState }

// Format timestamp to human-readable local time with timezone
const formatTimestampToLocalString = (timestamp: number): string => {
  const date = new Date(timestamp);
  // Use Swedish locale 'sv-SE' to get YYYY-MM-DD HH:mm:ss format
  const localTime = date.toLocaleString('sv-SE', { hour12: false });
  // Get timezone offset
  const offsetMinutes = -date.getTimezoneOffset();
  const offsetHours = Math.floor(Math.abs(offsetMinutes) / 60);
  const offsetSign = offsetMinutes >= 0 ? '+' : '-';
  return `${localTime} (UTC${offsetSign}${offsetHours})`;
};

const parseTokenPayload = (token: string): {
  sub?: string
  role?: string
  exp?: number
  memberships?: MembershipClaim[]
} => {
  try {
    // JWT tokens are in the format: header.payload.signature
    const parts = token.split('.');
    if (parts.length !== 3) return {};
    const payload = JSON.parse(atob(parts[1]));
    return {
      ...payload,
      memberships: normalizeMembershipClaims(payload.memberships),
    };
  } catch (e) {
    console.error('Error parsing token payload:', e);
    return {};
  }
};

const getUsernameFromToken = (token: string): string | null => {
  const payload = parseTokenPayload(token);
  return payload.sub || null;
};

const getRoleFromToken = (token: string): string | null => {
  const payload = parseTokenPayload(token);
  return payload.role || null;
};

const getTokenExpiresAt = (token: string): number | null => {
  const payload = parseTokenPayload(token);
  return payload.exp ? payload.exp * 1000 : null; // Convert to milliseconds
};

const getMembershipsFromToken = (token: string): MembershipClaim[] => {
  const payload = parseTokenPayload(token);
  return payload.memberships || [];
};

const initAuthState = (): { isAuthenticated: boolean; coreVersion: string | null; apiVersion: string | null; username: string | null; role: string | null; memberships: MembershipClaim[]; webuiTitle: string | null; webuiDescription: string | null; lastTokenRenewal: string | null; tokenExpiresAt: number | null } => {
  const token = localStorage.getItem('LIGHTRAG-API-TOKEN');
  const coreVersion = localStorage.getItem('LIGHTRAG-CORE-VERSION');
  const apiVersion = localStorage.getItem('LIGHTRAG-API-VERSION');
  const webuiTitle = localStorage.getItem('LIGHTRAG-WEBUI-TITLE');
  const webuiDescription = localStorage.getItem('LIGHTRAG-WEBUI-DESCRIPTION');
  const lastTokenRenewal = localStorage.getItem('LIGHTRAG-LAST-TOKEN-RENEWAL');
  const username = token ? getUsernameFromToken(token) : null;
  const role = token ? getRoleFromToken(token) : null;
  const memberships = token ? getMembershipsFromToken(token) : [];
  const tokenExpiresAt = token ? getTokenExpiresAt(token) : null;

  if (!token) {
    return {
      isAuthenticated: false,
      coreVersion: coreVersion,
      apiVersion: apiVersion,
      username: null,
      role: null,
      memberships: [],
      webuiTitle: webuiTitle,
      webuiDescription: webuiDescription,
      lastTokenRenewal: null,
      tokenExpiresAt: null,
    };
  }

  return {
    isAuthenticated: true,
    coreVersion: coreVersion,
    apiVersion: apiVersion,
    username: username,
    role: role,
    memberships: memberships,
    webuiTitle: webuiTitle,
    webuiDescription: webuiDescription,
    lastTokenRenewal: lastTokenRenewal,
    tokenExpiresAt: tokenExpiresAt,
  };
};

export const useAuthStore = create<AuthState>(set => {
  // Get initial state from localStorage
  const initialState = initAuthState();

  return {
    isAuthenticated: initialState.isAuthenticated,
    coreVersion: initialState.coreVersion,
    apiVersion: initialState.apiVersion,
    username: initialState.username,
    role: initialState.role,
    memberships: initialState.memberships,
    webuiTitle: initialState.webuiTitle,
    webuiDescription: initialState.webuiDescription,
    lastTokenRenewal: initialState.lastTokenRenewal,
    tokenExpiresAt: initialState.tokenExpiresAt,

    login: (token, coreVersion = null, apiVersion = null, webuiTitle = null, webuiDescription = null) => {
      localStorage.setItem('LIGHTRAG-API-TOKEN', token);

      if (coreVersion) {
        localStorage.setItem('LIGHTRAG-CORE-VERSION', coreVersion);
      }
      if (apiVersion) {
        localStorage.setItem('LIGHTRAG-API-VERSION', apiVersion);
      }

      if (webuiTitle) {
        localStorage.setItem('LIGHTRAG-WEBUI-TITLE', webuiTitle);
      } else {
        localStorage.removeItem('LIGHTRAG-WEBUI-TITLE');
      }

      if (webuiDescription) {
        localStorage.setItem('LIGHTRAG-WEBUI-DESCRIPTION', webuiDescription);
      } else {
        localStorage.removeItem('LIGHTRAG-WEBUI-DESCRIPTION');
      }

      const username = getUsernameFromToken(token);
      const role = getRoleFromToken(token);
      const memberships = getMembershipsFromToken(token);
      const tokenExpiresAt = getTokenExpiresAt(token);
      const now = Date.now();
      const formattedTime = formatTimestampToLocalString(now);

      // Initialize token issuance time with human-readable format
      localStorage.setItem('LIGHTRAG-LAST-TOKEN-RENEWAL', formattedTime);

      set({
        isAuthenticated: true,
        username: username,
        role: role,
        memberships: memberships,
        coreVersion: coreVersion,
        apiVersion: apiVersion,
        webuiTitle: webuiTitle,
        webuiDescription: webuiDescription,
        tokenExpiresAt: tokenExpiresAt,
        lastTokenRenewal: formattedTime,
      });
    },

    logout: () => {
      localStorage.removeItem('LIGHTRAG-API-TOKEN');
      localStorage.removeItem('LIGHTRAG-LAST-TOKEN-RENEWAL');

      const coreVersion = localStorage.getItem('LIGHTRAG-CORE-VERSION');
      const apiVersion = localStorage.getItem('LIGHTRAG-API-VERSION');
      const webuiTitle = localStorage.getItem('LIGHTRAG-WEBUI-TITLE');
      const webuiDescription = localStorage.getItem('LIGHTRAG-WEBUI-DESCRIPTION');

      set({
        isAuthenticated: false,
        username: null,
        role: null,
        memberships: [],
        coreVersion: coreVersion,
        apiVersion: apiVersion,
        webuiTitle: webuiTitle,
        webuiDescription: webuiDescription,
        lastTokenRenewal: null,
        tokenExpiresAt: null,
      });
    },

    setVersion: (coreVersion, apiVersion) => {
      // Update localStorage
      if (coreVersion) {
        localStorage.setItem('LIGHTRAG-CORE-VERSION', coreVersion);
      }
      if (apiVersion) {
        localStorage.setItem('LIGHTRAG-API-VERSION', apiVersion);
      }

      // Update state
      set({
        coreVersion: coreVersion,
        apiVersion: apiVersion
      });
    },

    grantMembershipClaim: (claim) => {
      // Idempotent: skip if we already have an identical (workspace_id,
      // kb_id) pair in the claims list. Otherwise append.
      const normalizedClaim: MembershipClaim = {
        workspace_id: claim.workspace_id,
        kb_id: claim.kb_id ?? null,
        role: claim.role,
      };
      const existing = initAuthState().memberships;
      const current = (useAuthStore.getState().memberships || existing);
      const alreadyPresent = current.some(
        (c) =>
          c.workspace_id === normalizedClaim.workspace_id &&
          (c.kb_id || null) === (normalizedClaim.kb_id || null)
      );
      if (alreadyPresent) return;
      set({ memberships: [...current, normalizedClaim] });
    },

    setCustomTitle: (webuiTitle, webuiDescription) => {
      // Update localStorage
      if (webuiTitle) {
        localStorage.setItem('LIGHTRAG-WEBUI-TITLE', webuiTitle);
      } else {
        localStorage.removeItem('LIGHTRAG-WEBUI-TITLE');
      }

      if (webuiDescription) {
        localStorage.setItem('LIGHTRAG-WEBUI-DESCRIPTION', webuiDescription);
      } else {
        localStorage.removeItem('LIGHTRAG-WEBUI-DESCRIPTION');
      }

      // Update state
      set({
        webuiTitle: webuiTitle,
        webuiDescription: webuiDescription
      });
    },

    setTokenRenewal: (renewalTime, expiresAt) => {
      const formattedTime = formatTimestampToLocalString(renewalTime);

      // Update localStorage with human-readable format
      localStorage.setItem('LIGHTRAG-LAST-TOKEN-RENEWAL', formattedTime);

      // Update state
      set({
        lastTokenRenewal: formattedTime,
        tokenExpiresAt: expiresAt
      });
    }
  };
});
