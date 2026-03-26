LIMPATEX - ZADARMA SCHEDULER

PASOS PARA EJECUTAR:

1. Instalar dependencias:
pip install requests python-dotenv

2. Crear archivo .env con:
ZADARMA_KEY=tu_api_key
ZADARMA_SECRET=tu_secret
NO_SHIFT_MODE=off

3. Ejecutar:
python scheduler.py

4. Para producción:
programar con cron cada 5 minutos

Ejemplo:
*/5 * * * * python /ruta/scheduler.py

NOTAS:
- NO subir claves a GitHub
- Revisar logs en scheduler.log