/**
 * Tiny toast notification store.
 *
 * Use `toast.error(msg)` / `toast.success(msg)` / `toast.info(msg)` from any
 * component; the layout-level <Toasts /> component renders them. Errors stick
 * around (no auto-dismiss) so users have time to read; successes/info fade.
 */

let nextId = 1;

export type ToastKind = 'error' | 'success' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
  /** ms; null = sticky (no auto-dismiss). Errors are sticky by default. */
  ttl: number | null;
}

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

  error(message: string, ttl: number | null = null): number {
    return this.push('error', message, ttl);
  }
  success(message: string, ttl: number | null = 3000): number {
    return this.push('success', message, ttl);
  }
  info(message: string, ttl: number | null = 4000): number {
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
