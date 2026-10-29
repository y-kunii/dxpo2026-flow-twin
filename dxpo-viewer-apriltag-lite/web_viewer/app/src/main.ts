// Keep the Vite entry point free of Viewer implementation details. The
// composition root remains lazy so application-only distributions do not
// evaluate Three.js or Spark until a Viewer route is opened.
void import("./ViewerApplication");
