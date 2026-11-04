# Asistent WhatsApp pentru programări (GitHub + Render, fără Python local)

## 1. Cheia Claude
1. https://console.anthropic.com > cont nou > Billing > adaugă 5 £ credit.
2. API keys > Create key. Copiaz-o.

## 2. Twilio WhatsApp Sandbox (gratuit)
1. Cont pe https://www.twilio.com/try-twilio
2. Din Console copiază **Account SID** și **Auth Token**.
3. Messaging > Try it out > Send a WhatsApp message.
4. De pe telefonul tău trimite pe WhatsApp către numărul afișat: `join <cuvintele-tale>`.

## 3. GitHub
1. Cont pe https://github.com > New repository > nume `whatsapp-booking-bot` > Create.
2. „uploading an existing file" > trage aici TOATE fișierele din folderul dezarhivat
   (app.py, requirements.txt, render.yaml, README.md, .gitignore) > Commit.
   NU urca fișierul .env.

## 4. Render
1. https://render.com > cont (poți intra cu GitHub) > New > **Web Service** > alege repo-ul.
2. Dacă nu ia singur setările din render.yaml, pune manual:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app --workers 1 --threads 4 --timeout 60`
   - Plan: Free
3. La **Environment** adaugă:
   - `ANTHROPIC_API_KEY` = cheia ta
   - `TWILIO_ACCOUNT_SID` și `TWILIO_AUTH_TOKEN`
   - `OWNER_WHATSAPP` = `whatsapp:+44...` (numărul tău, opțional)
4. Deploy. După 2-3 minute primești adresa, de ex. `https://whatsapp-booking-bot.onrender.com`.
   Deschide-o în browser: trebuie să scrie „Booking assistant is running."

## 5. Leagă Twilio de Render
Twilio > Messaging > Settings > WhatsApp Sandbox settings > **When a message comes in**:
`https://ADRESA-TA.onrender.com/whatsapp`  (metoda POST) > Save.

## 6. Testează
Scrie către numărul sandbox: „Hi, can I book a skin fade tomorrow afternoon?"

## De știut
- Planul gratuit Render adoarme după ~15 min fără trafic; primul mesaj poate dura ~50 de secunde. Pentru clienți reali: plan plătit (~7 $/lună).
- Pe planul gratuit programările se pierd la fiecare redeploy/restart. Pentru clienți trecem pe o bază de date permanentă.
- Sandbox-ul cere „join" la 72 de ore. Pentru clienți reali ai nevoie de un număr WhatsApp Business aprobat.
- Datele afacerii se schimbă în blocul `BUSINESS` din app.py.
