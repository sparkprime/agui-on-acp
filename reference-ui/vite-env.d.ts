/// <reference types="vite/client" />

interface BrandingConfig {
  projectName: string;
  displayTitle: string;
  description: string;
  backendPort: number;
  corsOrigins: string[];
}

declare const __BRANDING_CONFIG__: BrandingConfig;
