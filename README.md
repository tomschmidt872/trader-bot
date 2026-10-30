# Trading-Bot (Simulation)

Ein Bot, der mit **Spielgeld** handelt. Es gibt keinen Broker und kein echtes Geld.

Er läuft stündlich während der US-Börsenzeiten über GitHub Actions und trifft seine Entscheidungen auf dieser Grundlage:

- **Signale anderer**: 13F-Depots von Fonds (Berkshire Hathaway) und Aktiengeschäfte von Kongressmitgliedern (House PTRs)
- **Trend**: SMA 50/200, Golden/Death Cross, ADX
- **Momentum**: MACD, RSI, Stochastik, Bollinger-Bänder, 20-Tage-Rendite
- **Volumen**: relatives Volumen, OBV, Chaikin Money Flow, Kauf- vs. Verkaufsvolumen, 52-Wochen-Ausbruch
- **Intraday**: VWAP, Opening Range, Gap
- **News**: Stimmung der Schlagzeilen und wichtige 8-K-Meldungen
- **Marktlage**: S&P-500-Trend und VIX

Das Dashboard liegt in `docs/` und wird über GitHub Pages veröffentlicht. Die Einstellungen stehen in `trader/bot.json`.

Die Datenwerkzeuge in `skills/` stammen aus [trader-skill](https://github.com/Jakeschincariol/trader-skill) (MIT-Lizenz, siehe `skills/LICENSE`).

Keine Anlageberatung.
