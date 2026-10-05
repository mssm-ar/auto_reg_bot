/**
 * PM2 — simple registration bot (this directory).
 *
 *   pm2 start ecosystem.config.js
 *   pm2 logs simple-reg-bot
 *   pm2 restart simple-reg-bot
 *
 * Logs: ./logs/ (create with: mkdir -p logs)
 */
module.exports = {
  apps: [
    {
      name: "simple-reg-bot",
      script: "simple_reg_bot.py",
      interpreter: "python3",
      cwd: __dirname,
      autorestart: true,
      watch: false,
      max_restarts: 1000000,
      min_uptime: "10s",
      restart_delay: 2000,
      out_file: `${__dirname}/logs/simple-reg-bot-out.log`,
      error_file: `${__dirname}/logs/simple-reg-bot-err.log`,
      merge_logs: true,
      time: true,
      env: {
        CHECK_INTERVAL_SECONDS: "30",
      },
    },
  ],
};
