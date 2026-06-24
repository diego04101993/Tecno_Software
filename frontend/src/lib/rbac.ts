import type { UserRole } from "../types/domain";


export function isSuperAdmin(role: UserRole | null | undefined) {
  return role === "super_admin";
}


export function isStaffAdmin(role: UserRole | null | undefined) {
  return role === "staff_admin";
}


export function isStaffOperator(role: UserRole | null | undefined) {
  return role === "staff_operator";
}


export function canAccessGlobalClients(role: UserRole | null | undefined) {
  return isSuperAdmin(role) || isStaffAdmin(role) || isStaffOperator(role);
}


export function canManageClientDirectory(role: UserRole | null | undefined) {
  return isSuperAdmin(role) || isStaffAdmin(role);
}


export function canManageInternalTeam(role: UserRole | null | undefined) {
  return isSuperAdmin(role);
}


export function isClientAdminLike(role: UserRole | null | undefined) {
  return role === "client" || role === "client_admin" || role === "client_operator";
}


export function isClientOperator(role: UserRole | null | undefined) {
  return role === "client_operator";
}


export function isBranchManager(role: UserRole | null | undefined) {
  return role === "branch_manager";
}


export function isOperator(role: UserRole | null | undefined) {
  return role === "operator";
}


export function canWriteClientScope(role: UserRole | null | undefined) {
  return canAccessGlobalClients(role) || isClientAdminLike(role);
}


export function canWriteBranchScope(role: UserRole | null | undefined) {
  return canWriteClientScope(role) || isBranchManager(role);
}


export function canManageClientUsers(role: UserRole | null | undefined) {
  return canManageClientDirectory(role) || role === "client" || role === "client_admin";
}
