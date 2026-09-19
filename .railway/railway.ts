import { defineRailway, github, postgres, preserve, project, service, volume } from "railway/iac";

export default defineRailway(() => {
  const wbMiniApp = github("gorbunovadi1-sys/wb-mini-app", { checkSuites: false });

  const Postgres = postgres("Postgres", { region: "iad" });
  Postgres.networking = { privateNetworkEndpoint: "postgres" };
  const postgresVolume = volume("postgres-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "iad", sizeMB: 500 });
  const worker = service("worker", {
    source: wbMiniApp,
    replicas: { "iad": 1 },
    env: { AI_ENGINE_BOT_TOKEN: preserve(), DATABASE_URL: preserve(), ENCRYPTION_KEY: preserve() },
  });
  const web = service("web", {
    source: wbMiniApp,
    start: "uvicorn backend.ai_engine_app:app --host 0.0.0.0 --port $PORT",
    replicas: { "iad": 1 },
    env: {
      ADMIN_TELEGRAM_ID: preserve(), AI_ENGINE_BOT_TOKEN: preserve(), AI_ENGINE_MINI_APP_URL: preserve(),
      DATABASE_URL: preserve(), DEV_MODE: preserve(), ENCRYPTION_KEY: preserve(),
      // Мини-апп и API для Сельдереевой (backend/seldereeva_routes.py) живут
      // в этом же процессе — им нужны те же SELD_-переменные, что и у
      // seldereevaBot ниже (валидация initData, WB/Ozon клиенты).
      SELD_BOT_TOKEN: preserve(), SELD_WB_API_KEY: preserve(),
      SELD_OZON_CLIENT_ID: preserve(), SELD_OZON_API_KEY: preserve(), SELD_TAX_PCT: preserve(),
    },
  });
  // kim_bot уже объявлен в Procfile (kim_bot: python -m kim_bot.worker), но
  // ещё не описан здесь как сервис — не трогаю его в рамках этой задачи,
  // только новый seldereevaBot ниже; стоит поправить отдельно.
  const seldereevaBot = service("seldereevaBot", {
    source: wbMiniApp,
    start: "python -m seldereeva_bot.worker",
    replicas: { "iad": 1 },
    env: {
      SELD_BOT_TOKEN: preserve(), SELD_PERSONAL_CHAT_ID: preserve(), SELD_MANAGERS_CHAT_ID: preserve(),
      SELD_WB_API_KEY: preserve(), SELD_OZON_CLIENT_ID: preserve(), SELD_OZON_API_KEY: preserve(),
      SELD_TAX_PCT: preserve(), SELD_MINI_APP_URL: preserve(), DATABASE_URL: preserve(),
    },
  });

  return project("protective-rejoicing", {
    resources: [worker, Postgres, web, postgresVolume, seldereevaBot],
  });
});
