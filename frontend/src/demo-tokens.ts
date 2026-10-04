/**
 * 9 个演示身份令牌（3 租户 × 3 角色），离线硬编码可直接演示，不跑 seed 脚本也行。
 * 后端使用同一个 secret 生成/验证（见 backend/scripts/_gen_demo_tokens.py）。
 * secret 必须与根目录 .env 的 SECURITY__DEMO_TOKEN_SECRET 一致。
 */
export type Role = "consumer" | "staff" | "admin";

export interface DemoIdentity {
  tenant_id: string;
  tenant_name: string;
  role: Role;
  username: string;
  actor_id: string;
  access_token: string;
  expires_in_seconds: number;
}

export const DEMO_IDENTITIES: DemoIdentity[] = [
  {
    tenant_id: "tenant_a",
    tenant_name: "禅饰坊",
    role: "consumer",
    username: "chan-consumer",
    actor_id: "3f88a233-4d11-50e6-926b-e0ddd2838c0c",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiIxOGJiNzUyM2RjNjI0MWFlYThlMTY1MTk1MTEzMjNlYiIsInN1YiI6IjNmODhhMjMzLTRkMTEtNTBlNi05MjZiLWUwZGRkMjgzOGMwYyIsInRlbmFudF9pZCI6InRlbmFudF9hIiwicm9sZSI6ImNvbnN1bWVyIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.YX8614F4Dw-_KNltXcFooQsQMtx_9_9LVloQWwHNqfE",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_a",
    tenant_name: "禅饰坊",
    role: "staff",
    username: "chan-staff",
    actor_id: "7dab33cd-d516-5f11-8692-28554fa26646",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiIxNDJlZWZlZThmMzM0OWVlYjBhZjM2Zjg5NjFjYzAxMyIsInN1YiI6IjdkYWIzM2NkLWQ1MTYtNWYxMS04NjkyLTI4NTU0ZmEyNjY0NiIsInRlbmFudF9pZCI6InRlbmFudF9hIiwicm9sZSI6InN0YWZmIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.sWgD4US8YVTaIHxvSmaCcfcletI3ygrKOTJufArljm0",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_a",
    tenant_name: "禅饰坊",
    role: "admin",
    username: "chan-admin",
    actor_id: "175a13f9-0375-5b6f-83db-e0ffb4dc6674",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiI5MGZlZWIxZTMxNjg0OTZiYmI3ZWY4MGJhZmI1MTFkOCIsInN1YiI6IjE3NWExM2Y5LTAzNzUtNWI2Zi04M2RiLWUwZmZiNGRjNjY3NCIsInRlbmFudF9pZCI6InRlbmFudF9hIiwicm9sZSI6ImFkbWluIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.QIFQDOfJGThxeOUzDhaxXrPeEC7rsR7V2041S6FfPl8",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_b",
    tenant_name: "梵印阁",
    role: "consumer",
    username: "fy-consumer",
    actor_id: "d5f58850-d733-5150-9b07-f96fa2d53b41",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiI2MDY4YmU4NDI3N2Q0Mzc1YmE0NTcxNGViYTNiNzhkNiIsInN1YiI6ImQ1ZjU4ODUwLWQ3MzMtNTE1MC05YjA3LWY5NmZhMmQ1M2I0MSIsInRlbmFudF9pZCI6InRlbmFudF9iIiwicm9sZSI6ImNvbnN1bWVyIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.fzE9z0J-yHW9R9JE_rpG7EUBUcAmPHWZhzvvxHWZHgc",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_b",
    tenant_name: "梵印阁",
    role: "staff",
    username: "fy-staff",
    actor_id: "d852ecfc-e419-528c-ad42-afa0ed273993",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiIzMGVlYTEwMmYzOWY0NWIxOTczYzZjNmI0ODdhZGI4NyIsInN1YiI6ImQ4NTJlY2ZjLWU0MTktNTI4Yy1hZDQyLWFmYTBlZDI3Mzk5MyIsInRlbmFudF9pZCI6InRlbmFudF9iIiwicm9sZSI6InN0YWZmIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.wRPhja0XzXh4OnSDHEzftDUzUjHhfZYYIU7dTAH_V94",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_b",
    tenant_name: "梵印阁",
    role: "admin",
    username: "fy-admin",
    actor_id: "84d8de71-cc60-50c7-a9e3-4fdc7c4e63cf",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJmYWNlZjczYWNjYTk0YTc4OWYyYzcwOTQ2ZGY4Y2NlNyIsInN1YiI6Ijg0ZDhkZTcxLWNjNjAtNTBjNy1hOWUzLTRmZGM3YzRlNjNjZiIsInRlbmFudF9pZCI6InRlbmFudF9iIiwicm9sZSI6ImFkbWluIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.wTnMoWNWR3-DbiWL1ukLfixuuJeYJlyYgF9fBC3ujlY",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_c",
    tenant_name: "玉语轩",
    role: "consumer",
    username: "yy-consumer",
    actor_id: "ce37ef2f-e00a-54b3-ace9-9eb2cb673409",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJkMWRmMmEzYTVhNmI0ZTUwOGQxMTlkN2IzYjYxZTY2MiIsInN1YiI6ImNlMzdlZjJmLWUwMGEtNTRiMy1hY2U5LTllYjJjYjY3MzQwOSIsInRlbmFudF9pZCI6InRlbmFudF9jIiwicm9sZSI6ImNvbnN1bWVyIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.iez3UyUnY8oZc9I8DmxdRbqGEceq9CHOOpfizcNMGLg",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_c",
    tenant_name: "玉语轩",
    role: "staff",
    username: "yy-staff",
    actor_id: "f1e1d992-35e2-51e1-97ea-87de0297428a",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJlOTNhNDI3MTlhYjE0YjA4YWUwYzViMmJjNzJmZGI0OSIsInN1YiI6ImYxZTFkOTkyLTM1ZTItNTFlMS05N2VhLTg3ZGUwMjk3NDI4YSIsInRlbmFudF9pZCI6InRlbmFudF9jIiwicm9sZSI6InN0YWZmIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.Ko_zPn2dJQSGQsOB0yjH7V6z2o9mvjhs2LitO8r3bsY",
    expires_in_seconds: 2592000,
  },
  {
    tenant_id: "tenant_c",
    tenant_name: "玉语轩",
    role: "admin",
    username: "yy-admin",
    actor_id: "f3eb0d91-a41e-500a-a867-a8314850c723",
    access_token:
      "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiI1NTFkNzNhNGU5ZmI0MjJlYWM3NTJjYjVlZGFiOWFhNSIsInN1YiI6ImYzZWIwZDkxLWE0MWUtNTAwYS1hODY3LWE4MzE0ODUwYzcyMyIsInRlbmFudF9pZCI6InRlbmFudF9jIiwicm9sZSI6ImFkbWluIiwiaWF0IjoxNzg5Mzg3OTE5LCJleHAiOjE3OTE5Nzk5MTksInR5cGUiOiJkZW1vX2FjY2VzcyJ9.y5G0UrNd7sYjsxGE_FCfbDtmsNWweiP-r2aGcTuJPJ4",
    expires_in_seconds: 2592000,
  },
];

export const DEFAULT_IDENTITY: DemoIdentity = DEMO_IDENTITIES[0];
