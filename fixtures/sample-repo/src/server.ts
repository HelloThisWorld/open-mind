/**
 * HTTP entry point for the sample order service.
 * Boots the app, mounts the order routes, and listens on port 8080.
 */
import { HttpApp } from "./http-app";
import { registerOrderRoutes } from "./routes/orders";

export function startServer(): HttpApp {
  const app = new HttpApp();
  registerOrderRoutes(app);
  app.listen(8080);
  return app;
}

startServer();
