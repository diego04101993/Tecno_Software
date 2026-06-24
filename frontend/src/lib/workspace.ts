import type { SessionUser } from "../types/domain";
import { canAccessGlobalClients, isBranchManager, isClientAdminLike, isOperator } from "./rbac";

export type LegacySection = "dashboard" | "branches" | "channels" | "campaigns" | "contents" | "videowalls" | "kiosk";

export function buildClientOverviewPath(clientId: string) {
  return `/app/clients/${clientId}/overview`;
}

export function buildClientOperationsPath(clientId: string) {
  return `/app/clients/${clientId}/operations`;
}

export function buildClientBranchesPath(clientId: string) {
  return `/app/clients/${clientId}/branches`;
}

export function buildClientCampaignsPath(clientId: string) {
  return `/app/clients/${clientId}/campaigns`;
}

export function buildClientContentsPath(clientId: string) {
  return `/app/clients/${clientId}/contents`;
}

export function buildClientVideowallsPath(clientId: string) {
  return `/app/clients/${clientId}/videowalls`;
}

export function buildClientKioskPath(clientId: string) {
  return `/app/clients/${clientId}/kiosk`;
}

export function buildClientTouchPath(clientId: string) {
  return `/app/clients/${clientId}/kiosk/touch`;
}

export function buildTouchExperiencePath(clientId: string, experienceId: string) {
  return `/app/clients/${clientId}/kiosk/touch/${experienceId}`;
}

export function buildClientUsersPath(clientId: string) {
  return `/app/clients/${clientId}/users`;
}

export function buildClientAudioPath(clientId: string) {
  return `/app/clients/${clientId}/audio`;
}

export function buildClientDataSourcesPath(clientId: string) {
  return `/app/clients/${clientId}/data-sources`;
}

export function buildDatasetDetailPath(clientId: string, datasetId: string) {
  return `/app/clients/${clientId}/data-sources/${datasetId}`;
}

export function buildBranchOverviewPath(clientId: string, branchId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/overview`;
}

export function buildBranchChannelsPath(clientId: string, branchId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/channels`;
}

export function buildBranchTimelinePath(clientId: string, branchId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/timeline`;
}

export function buildBranchLayoutsPath(clientId: string, branchId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/layouts`;
}

export function buildLayoutEditorPath(clientId: string, branchId: string, layoutId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/layouts/${layoutId}/editor`;
}

export function buildLayoutPreviewPath(clientId: string, branchId: string, layoutId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/layouts/${layoutId}/preview`;
}

export function buildBranchPreviewPath(clientId: string, branchId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/preview`;
}

export function buildBranchAudioPath(clientId: string, branchId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/audio`;
}

export function buildChannelDetailPath(clientId: string, branchId: string, channelId: string) {
  return `/app/clients/${clientId}/branches/${branchId}/channels/${channelId}`;
}

export function canAccessClientWorkspace(user: SessionUser | null | undefined, clientId: string) {
  if (!user) {
    return false;
  }
  if (canAccessGlobalClients(user.role)) {
    return true;
  }
  if (isClientAdminLike(user.role)) {
    return user.client_id === clientId;
  }
  return false;
}

export function canAccessBranchWorkspace(user: SessionUser | null | undefined, clientId: string, branchId: string) {
  if (!user) {
    return false;
  }
  if (canAccessGlobalClients(user.role)) {
    return true;
  }
  if (isClientAdminLike(user.role)) {
    return user.client_id === clientId;
  }
  if (isBranchManager(user.role) || isOperator(user.role)) {
    return user.client_id === clientId && user.branch_id === branchId;
  }
  return false;
}

export function getDefaultAppPath(user: SessionUser | null | undefined) {
  if (!user) {
    return "/login";
  }
  if (canAccessGlobalClients(user.role)) {
    return "/app/clients";
  }
  if ((isBranchManager(user.role) || isOperator(user.role)) && user.client_id && user.branch_id) {
    return buildBranchOverviewPath(user.client_id, user.branch_id);
  }
  if (isClientAdminLike(user.role) && user.client_id) {
    return buildClientOverviewPath(user.client_id);
  }
  return "/app/clients";
}

export function getLegacyRoutePath(section: LegacySection, user: SessionUser | null | undefined) {
  if (!user) {
    return "/login";
  }

  const defaultPath = getDefaultAppPath(user);
  const hasClient = Boolean(user.client_id);
  const hasBranch = Boolean(user.client_id && user.branch_id);

  switch (section) {
    case "dashboard":
      return defaultPath;
    case "branches":
      if (hasBranch && user.client_id && user.branch_id) {
        return buildBranchOverviewPath(user.client_id, user.branch_id);
      }
      if (hasClient && user.client_id) {
        return buildClientBranchesPath(user.client_id);
      }
      return "/app/clients";
    case "channels":
      if (hasBranch && user.client_id && user.branch_id) {
        return buildBranchChannelsPath(user.client_id, user.branch_id);
      }
      if (hasClient && user.client_id) {
        return buildClientBranchesPath(user.client_id);
      }
      return "/app/clients";
    case "campaigns":
      if (hasBranch && user.client_id && user.branch_id) {
        return buildBranchTimelinePath(user.client_id, user.branch_id);
      }
      if (hasClient && user.client_id) {
        return buildClientCampaignsPath(user.client_id);
      }
      return "/app/clients";
    case "contents":
      if (hasClient && user.client_id && !isBranchManager(user.role) && !isOperator(user.role)) {
        return buildClientContentsPath(user.client_id);
      }
      return defaultPath;
    case "videowalls":
      if (hasClient && user.client_id && !isBranchManager(user.role) && !isOperator(user.role)) {
        return buildClientVideowallsPath(user.client_id);
      }
      return defaultPath;
    case "kiosk":
      if (hasClient && user.client_id && !isBranchManager(user.role) && !isOperator(user.role)) {
        return buildClientKioskPath(user.client_id);
      }
      return defaultPath;
    default:
      return defaultPath;
  }
}
