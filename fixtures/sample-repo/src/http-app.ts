/**
 * Minimal HTTP application shim so the fixture has zero dependencies.
 * The analysis cares about call shapes and definitions, not real sockets.
 */
export type RouteHandler = (body: Record<string, unknown>) => Promise<unknown>;

export class HttpApp {
  private readonly routes = new Map<string, RouteHandler>();

  post(path: string, handler: RouteHandler): void {
    this.routes.set(`POST ${path}`, handler);
  }

  listen(port: number): void {
    void port; // the fixture never opens a socket
  }
}
