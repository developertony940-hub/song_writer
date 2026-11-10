import { createBriefWorkspace } from './production-brief-core.mjs?v=2';

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  createBriefWorkspace().start();
}
