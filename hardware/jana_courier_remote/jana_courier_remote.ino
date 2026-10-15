/*
  Jana Navigator — физический пульт курьера на ESP32-S3.

  Из базового набора используются: кнопка, RGB-светодиод, активный зуммер.
  Микрофона в наборе нет — плата НЕ пишет звук сама, она лишь удалённо
  запускает запись на телефоне (см. backend/routers/hardware.py и
  frontend/app.js) и показывает статус цветом/звуком.

  Требуемые библиотеки (Arduino Library Manager):
    - ArduinoJson (Benoit Blanchon)
  WiFi.h и HTTPClient.h идут в комплекте с ESP32 Arduino core.

  ВАЖНО:
  - Сверьте номера пинов с распиновкой ВАШЕЙ конкретной платы ESP32-S3 —
    дешёвые "mini" платы от разных продавцов маркируются по-разному.
  - Если у вас Arduino-ESP32 core 3.x, синтаксис ledcAttachPin/ledcSetup
    мог измениться на ledcAttach(pin, freq, resolution) — проверьте
    changelog вашей версии core перед прошивкой.
  - На демо используйте СВОЮ точку доступа (сам ESP32 в режиме AP или
    отдельный роутер), а не WiFi площадки — это главный источник сбоев
    у железных демо.
*/

#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ---- Настройки, заполните перед прошивкой ----
const char* WIFI_SSID     = "ВАШ_WIFI_ИЛИ_ТОЧКА_ДОСТУПА";
const char* WIFI_PASSWORD = "ПАРОЛЬ";
const char* BACKEND_HOST  = "http://192.168.4.1:8000"; // IP машины с бэкендом в демо-сети

// ---- Пины (сверьте с распиновкой вашей платы) ----
const int PIN_BUTTON = 4;   // кнопка на GND, INPUT_PULLUP
const int PIN_LED_R  = 5;
const int PIN_LED_G  = 6;
const int PIN_LED_B  = 7;
const int PIN_BUZZER = 15;  // активный зуммер

const int CH_R = 0, CH_G = 1, CH_B = 2; // PWM-каналы LEDC для RGB

enum Status { IDLE, LISTENING, PROCESSING, SUCCESS, ERROR };
Status currentStatus = IDLE;

void setColor(int r, int g, int b) {
  ledcWrite(CH_R, r);
  ledcWrite(CH_G, g);
  ledcWrite(CH_B, b);
}

void beep(int durationMs) {
  digitalWrite(PIN_BUZZER, HIGH);
  delay(durationMs);
  digitalWrite(PIN_BUZZER, LOW);
}

void applyStatusVisuals(Status s) {
  switch (s) {
    case IDLE:
      setColor(0, 0, 60);          // приглушённый синий — ожидание
      break;
    case LISTENING:
    case PROCESSING:
      setColor(200, 140, 0);       // золото — в цвет is-recording из UI
      break;
    case SUCCESS:
      setColor(20, 150, 60);       // зелёный
      beep(120);
      break;
    case ERROR:
      setColor(180, 30, 30);       // красный
      beep(80); delay(80); beep(80);
      break;
  }
}

void connectWiFi() {
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Подключение к WiFi");
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(300);
    Serial.print(".");
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println(" OK, IP: " + WiFi.localIP().toString());
  } else {
    Serial.println(" не удалось подключиться — проверьте SSID/пароль/дальность");
  }
}

void triggerBackend() {
  HTTPClient http;
  http.begin(String(BACKEND_HOST) + "/api/hardware/trigger");
  http.addHeader("Content-Type", "application/json");
  int code = http.POST("{}");
  http.end();
  currentStatus = (code == 200) ? LISTENING : ERROR;
}

Status pollStatus() {
  HTTPClient http;
  http.begin(String(BACKEND_HOST) + "/api/hardware/status");
  int code = http.GET();
  Status result = currentStatus;

  if (code == 200) {
    StaticJsonDocument<128> doc;
    DeserializationError err = deserializeJson(doc, http.getString());
    if (!err) {
      String s = doc["status"].as<String>();
      if (s == "listening")  result = LISTENING;
      else if (s == "processing") result = PROCESSING;
      else if (s == "success")    result = SUCCESS;
      else if (s == "error")      result = ERROR;
      else if (s == "idle")       result = IDLE;
    }
  }
  http.end();
  return result;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  pinMode(PIN_BUZZER, OUTPUT);

  ledcSetup(CH_R, 5000, 8); ledcAttachPin(PIN_LED_R, CH_R);
  ledcSetup(CH_G, 5000, 8); ledcAttachPin(PIN_LED_G, CH_G);
  ledcSetup(CH_B, 5000, 8); ledcAttachPin(PIN_LED_B, CH_B);

  connectWiFi();
  applyStatusVisuals(IDLE);
}

unsigned long lastPoll = 0;
bool sessionActive = false;

void loop() {
  // Кнопка нажата (active LOW), сессии сейчас нет — стартуем
  if (digitalRead(PIN_BUTTON) == LOW && !sessionActive) {
    delay(30); // простая антидребезговая пауза
    if (digitalRead(PIN_BUTTON) == LOW) {
      sessionActive = true;
      triggerBackend();
      applyStatusVisuals(currentStatus);
      lastPoll = millis();
    }
  }

  // Пока сессия активна — опрашиваем бэкенд раз в 300мс
  if (sessionActive && millis() - lastPoll > 300) {
    lastPoll = millis();
    Status s = pollStatus();
    if (s != currentStatus) {
      currentStatus = s;
      applyStatusVisuals(s);
    }
    if (s == SUCCESS || s == ERROR) {
      delay(1500);
      sessionActive = false;
      currentStatus = IDLE;
      applyStatusVisuals(IDLE);
    }
  }
}
