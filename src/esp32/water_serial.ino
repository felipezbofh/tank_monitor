#include <WiFi.h>
#include <WiFiManager.h>
#include <HTTPClient.h>
#include <AsyncDelay.h>
#include <jsnsr04t.h>
#include <ArduinoJson.h>

// --- HARDWARE PIN DEFINITIONS ---
#define TRIG_PIN_1 4
#define ECHO_PIN_1 5

#define TRIG_PIN_2 6
#define ECHO_PIN_2 7

// --- LINUX SERVER CONFIGURATION ---
const char* linuxServerUrl = "http://192.168.1.196:5000/api/reading";

// --- DEEP SLEEP CONFIGURATION ---
#define uS_TO_S_FACTOR 1000000ULL /* Microseconds conversion factor */
#define TIME_TO_SLEEP  60        /* Sleep time in seconds (900s = 15 mins) */

// Hardware Instances
JsnSr04T sonar1(ECHO_PIN_1, TRIG_PIN_1);
JsnSr04T sonar2(ECHO_PIN_2, TRIG_PIN_2);

// Reads distance using the DevGiants JsnSr04T library
float readTankDistance(JsnSr04T& sonar) {
  delay(100);

  for (int attempt = 0; attempt < 3; attempt++) {
    float distCm = sonar.readDistance(); 
    if (distCm > 0 && distCm <= 400.0) {
      return distCm;
    }
    delay(150);
  }
  return 0.0;
}

// Sends sensor reading to the Linux Flask server
void sendReadingToServer(const String& tankId, float distanceCm) {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("[%s] Skipping POST - Wi-Fi disconnected.\n", tankId.c_str());
    return;
  }

  // Validate reading before sending
  if (distanceCm <= 0) {
    Serial.printf("[%s] Invalid reading (%.1f). Skipping.\n", tankId.c_str(), distanceCm);
    return;
  }

  HTTPClient http;
  http.begin(linuxServerUrl);
  http.addHeader("Content-Type", "application/json");

  // Use ArduinoJson for safety and cleanliness
  StaticJsonDocument<128> doc;
  doc["tank_id"] = tankId;
  doc["distance"] = distanceCm;

  String jsonPayload;
  serializeJson(doc, jsonPayload);

  int httpResponseCode = http.POST(jsonPayload);
  if (httpResponseCode > 0) {
    Serial.printf("[%s] Successfully posted %.1f cm (HTTP %d)\n", tankId.c．c_str(), distanceCm, httpResponseCode);
  } else {
    Serial.printf("[%s] POST Failed: %s\n", tankId.c_str(), http.errorToString(httpResponseCode).c_str());
  }

  http.end();
}

void setup() {
  Serial.begin(115200);

  unsigned long start = millis();
  while (!Serial && (millis() - start < 3000)) delay(10);

  Serial.println("\n=================================");
  Serial.println("ESP32 Tank Monitor - Deep Sleep Mode");
  Serial.println("=================================");

  // Initialize sensors
  sonar1.begin(Serial);
  sonar2.begin(Serial);

  // Connect Wi-Fi
  WiFiManager wifiManager;
  wifiManager.setConfigPortalTimeout(120);

  if (!wifiManager.autoConnect("ESP32_Tank_Monitor_AP")) {
    Serial.println("Wi-Fi connection failed. Entering sleep anyway...");
  } else {
    Serial.print("Wi-Fi Connected! IP: ");
    Serial.println(WiFi.localIP());

    // Take measurement
    float dist1 = readTankDistance(sonar1);
    delay(200);
    float dist2 = readTankDistance(sonar2);

    Serial.printf("Tank 1: %.1f cm | Tank 2: %.1f cm\n", dist1, dist2);

    // Send data to server (Server handles email filtering and thresholds)
    if (dist1 > 0) sendReadingToServer("tank1", dist1);
    if (dist2 > 0) sendReadingToServer("tank2", dist2);
  }

  // Configure deep sleep timer and enter sleep
  Serial.printf("Entering deep sleep for %d seconds...\n", TIME_TO_SLEEP);
  Serial.flush();

  esp_sleep_enable_timer_wakeup(TIME_TO_SLEEP * uS_TO_S_FACTOR);
  esp_deep_sleep_start();
}

void loop() {
  // Never executed because execution resets to setup() on deep sleep wakeup
}