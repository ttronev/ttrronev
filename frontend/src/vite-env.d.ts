/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** "1" when the client serves fixtures from src/mock instead of /api/v1. */
  readonly VITE_MOCK?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
