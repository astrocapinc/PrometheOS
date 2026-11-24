// The browser boundary controls (decision 0028), against the real hub that global-setup started.
// This file is the `boundary` project (e2e/boundary.ts), a dependency of every journey: it runs
// before them under any spec or --grep filter, and a failing control means no journey runs.
import { registerBoundaryChecks } from "./boundary-checks";
import { origin, readHub } from "./fixtures";

registerBoundaryChecks(() => origin(readHub()));
