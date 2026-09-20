import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // S3 정적 호스팅에서 상대 경로로 로드
  base: "./",
});
