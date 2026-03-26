#!/bin/bash
# CRYPTEX — Setup & Start Script
# Einmalig ausführen: bash setup.sh

echo "=== CRYPTEX SETUP ==="

# Abhängigkeiten installieren
pip install flask flask-socketio requests eventlet --break-system-packages

# Discord Secret abfragen
echo ""
echo "Gib deinen Discord Client Secret ein (von discord.com/developers):"
read -s DISCORD_SECRET
export DISCORD_SECRET

echo ""
echo "=== CRYPTEX startet... ==="
echo "Erreichbar unter: http://cryptex-gta.duckdns.org"
echo "Admin Login: admin / admin123"
echo "Bitte Passwort sofort ändern!"
echo ""

python3 app.py
