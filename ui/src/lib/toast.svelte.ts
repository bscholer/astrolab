/**
 * Tiny toast notification store.
 *
 * Use `toast.error(msg)` / `toast.success(msg)` / `toast.info(msg)` from any
 * component; the layout-level <Toasts /> component renders them. All kinds
 * auto-dismiss; errors stick around longer so the user has time to read,
 * successes/info clear quicker so they don't pile up. Pass `null` as the
 * second arg to make a specific toast sticky for unusual cases (eg the
 * Mac/Linux build mismatch banner).
 */

let nextId = 1;

export type ToastKind = 'error' | 'success' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  /** ms; null = sticky (no auto-dismiss). */
  ttl: number | null;
}

// Default lifetimes per kind. Errors get the longest window because they
// usually carry information the user needs to act on; success is the
// shortest because it's just confirmation.
const TTL_ERROR = 10_000;
const TTL_INFO = 7_000;
const TTL_SUCCESS = 5_000;

class ToastStore {
  items = $state<Toast[]>([]);

  push(kind: ToastKind, message: string, ttl: number | null): number {
    const id = nextId++;
    this.items = [...this.items, { id, kind, message, ttl }];
    if (ttl !== null) {
      setTimeout(() => this.dismiss(id), ttl);
    }
    return id;
  }

  error(message: string, ttl: number | null = TTL_ERROR): number {
    return this.push('error', message, ttl);
  }
  success(message: string, ttl: number | null = TTL_SUCCESS): number {
    return this.push('success', message, ttl);
  }
  info(message: string, ttl: number | null = TTL_INFO): number {
    return this.push('info', message, ttl);
  }

  dismiss(id: number): void {
    this.items = this.items.filter((t) => t.id !== id);
  }

  clear(): void {
    this.items = [];
  }
}

export const toast = new ToastStore();
