/** @type {import('next').NextConfig} */
const backend = (
  process.env.WAYSTONE_API_PROXY ||
  process.env.NEXT_PUBLIC_API_BASE ||
  "http://127.0.0.1:9200"
).replace(/\/$/, "");

function isLoopbackHost(host) {
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function shouldProxyApi(url) {
  try {
    return isLoopbackHost(new URL(url).hostname);
  } catch {
    return false;
  }
}

const nextConfig = {
  output: "standalone",
  allowedDevOrigins: ["127.0.0.1", "localhost", "[::1]"],
  async rewrites() {
    // Local preview: browser talks only to the UI origin; Next forwards /api to FastAPI.
    // Production images bake a public NEXT_PUBLIC_API_BASE — ingress already routes /api.
    if (!shouldProxyApi(backend)) return [];
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};

export default nextConfig;
