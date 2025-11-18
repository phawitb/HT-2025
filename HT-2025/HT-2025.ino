/*Connect
HT22
HT22 <---> D1
VCC <---> 3V3,
GND <---> GND

LED2.4Display
LED <---> 3V3
CS  <---> D2 
RST <---> D3 
D/C <---> D4 
MOSI <---> D7 
SCK <---> D5 
VCC <---> 3V3
GND <---> GND
*/

#include <WiFiManager.h> // https://github.com/tzapu/WiFiManager
#include <WiFiUdp.h>
#include "DHT.h"
#include <Adafruit_GFX.h>    // Core graphics library
#include <Adafruit_ST7789.h> // Hardware-specific library for ST7789
#include <SPI.h>
#include <ESP8266HTTPClient.h>
#include <WiFiClientSecureBearSSL.h>
#include <NTPClient.h>
#include <ArduinoJson.h>

String DEVICE_ID = "aB4xT";  //edit=====================================
#define SCREEN_VERSION 1    //edit=====================================

// ======= FastAPI base URL (ใช้ตัวนี้ตัวเดียว) =======
const char* FASTAPI_BASE = "https://ht-2025.onrender.com";

// ======= interval ส่งเข้า /history ทุก 10 นาที =======
const unsigned long INTERVAL_SEC = 10UL * 60UL;

#define TFT_CS   D2
#define TFT_RST  D3
#define TFT_DC   D4
#define DHTPIN   D1
#define DHTTYPE  DHT22

#if SCREEN_VERSION == 1
  #define ST77XX_B    0X04FF
  #define ST77XX_M    0XF81F
  #define ST77XX_CYAN 0X07FF
#elif SCREEN_VERSION == 2
  #define ST77XX_B      0X04FF
  #define ST77XX_M      0x07E0
  #define ST77XX_BLACK  0xFFFF
  #define ST77XX_RED    0xFFE0
  #define ST77XX_GREEN  0xF81F
  #define ST77XX_WHITE  0x0000
  #define ST77XX_BLUE   0xF800
  #define ST77XX_CYAN   0x07FF
  #define ST77XX_MAGENTA 0xF81F
#endif

bool firsttime = true;
float humid, temp, hic, water, pm25;
int train, rest, timestamp;
String flag, status;

float adjust_temp  = 0;
float adjust_humid = 0;

const long offsetTime = 25200;       // 7 * 60 * 60 (TH time zone)
String UNIT;

// ไม่ใช้แล้ว (Google Sheet / LINE)
// String LINE_TOKEN1, LINE_TOKEN2, LINE_TOKEN3;
// float adjust_pm25 = 0;
// String sheet_api = "";
// String url1, url2;
bool SEND_DATA = false;
String message;

WiFiUDP ntpUDP;
Adafruit_ST7789 tft = Adafruit_ST7789(TFT_CS, TFT_DC, TFT_RST);
DHT dht(DHTPIN, DHTTYPE);
WiFiManager wm;
NTPClient timeClient(ntpUDP, "pool.ntp.org", offsetTime);

// --------------------------------------------------
// Helper parse JSON config จาก /config
// --------------------------------------------------
void parseJsonString(String text) {
  StaticJsonDocument<512> doc;  // เผื่อใหญ่หน่อย

  DeserializationError error = deserializeJson(doc, text);
  if (error) {
    Serial.print(F("deserializeJson() failed: "));
    Serial.println(error.f_str());
    return;
  }

  // structure ที่ FastAPI /config ส่งกลับมา (ตามที่เราออกแบบ)
  // {
  //   "success": true,
  //   "id": "S004",
  //   "unit": "...",
  //   "adj_temp": 0.0,
  //   "adj_humid": 0.0
  // }

  UNIT        = doc["unit"]     | String("");
  adjust_temp = doc["adj_temp"] | 0.0f;
  adjust_humid= doc["adj_humid"]| 0.0f;

  Serial.println("== Parsed config from /config ==");
  Serial.print("UNIT: ");        Serial.println(UNIT);
  Serial.print("adj_temp: ");    Serial.println(adjust_temp);
  Serial.print("adj_humid: ");   Serial.println(adjust_humid);
}

// --------------------------------------------------
// ดึง config จาก FastAPI: GET /config?id=DEVICE_ID
// --------------------------------------------------
void read_config_from_api() {
  std::unique_ptr<BearSSL::WiFiClientSecure> client(new BearSSL::WiFiClientSecure);
  client->setInsecure();
  HTTPClient https1;

  String url = String(FASTAPI_BASE) + "/config?id=" + DEVICE_ID;

  Serial.println("Reading config from FastAPI...");
  Serial.println(url);

  https1.begin(*client, url);
  https1.setFollowRedirects(HTTPC_STRICT_FOLLOW_REDIRECTS);
  int httpCode = https1.GET();

  Serial.print("HTTP Status Code (config): ");
  Serial.println(httpCode);

  if (httpCode <= 0) {
    Serial.println("Error on HTTP request (config)");
    https1.end();
    return;
  }

  String payload = https1.getString();
  Serial.println("Config payload: " + payload);

  if (httpCode == 200) {
    message = payload;
    parseJsonString(message);
  }

  https1.end();
}

// --------------------------------------------------
// POST /history ไปที่ FastAPI
// --------------------------------------------------
int post_history_api(String device_id, float temp, float humid, float hic, String flag_lower) {
  std::unique_ptr<BearSSL::WiFiClientSecure> client(new BearSSL::WiFiClientSecure);
  client->setInsecure();
  HTTPClient https;

  String url = String(FASTAPI_BASE) + "/history";
  Serial.println("POST " + url);

  if (https.begin(*client, url)) {
    // ใช้ epoch จาก NTP เป็น timestamp (ฝั่ง FastAPI รองรับเลข timestamp อยู่แล้ว)
    unsigned long epoch = timeClient.getEpochTime();

    String p = "{";
    p += "\"id\":\"" + device_id + "\",";
    p += "\"temp\":" + String(temp, 2) + ",";
    p += "\"humid\":" + String(humid, 2) + ",";
    p += "\"hic\":" + String(hic, 2) + ",";
    p += "\"flag\":\"" + flag_lower + "\",";
    p += "\"timestamp\":\"" + String(epoch) + "\"";
    p += "}";

    https.addHeader("Content-Type", "application/json");
    int httpResponseCode = https.POST(p);
    String content = https.getString();

    Serial.print("\nhttpResponseCode: ");
    Serial.println(httpResponseCode);
    Serial.print("content: ");
    Serial.println(content);

    https.end();
    return httpResponseCode;
  } else {
    Serial.printf("[HTTPS] Unable to connect\n");
    return -1;
  }
}

// --------------------------------------------------
// ฟังก์ชันวาดค่าบนหน้าจอ
// --------------------------------------------------
void updateScreen(String flag, float temp, float humid, String status, int bat_percentage) {
  tft.fillScreen(ST77XX_BLACK);

  if (status == "online") {
    tft.setTextColor(ST77XX_M);
  } else {
    tft.setTextColor(ST77XX_RED);
  }

  if (temp == -99) {
    tft.setTextColor(ST77XX_BLACK);
  }

  tft.setCursor(0, 30);
  tft.setTextSize(10);
  tft.print(String(int(floor(temp))));
  tft.setTextSize(3);
  tft.print("O");

  tft.setTextColor(ST77XX_B);
  if (temp == -99) {
    tft.setTextColor(ST77XX_BLACK);
  }

  tft.setCursor(167, 30);
  tft.setTextSize(10);
  tft.print(String(int(floor(humid))));
  tft.setTextSize(4);
  tft.print("%");

  tft.setTextSize(4);
  tft.setCursor(110, 72);

  if (status == "online") {
    tft.setTextColor(ST77XX_M);
  } else {
    tft.setTextColor(ST77XX_RED);
  }
  if (temp == -99) {
    tft.setTextColor(ST77XX_BLACK);
  }
  tft.print("." + String(int(10 * (temp - floor(temp)))));

  tft.setCursor(270, 72);
  tft.setTextColor(ST77XX_B);
  if (temp == -99) {
    tft.setTextColor(ST77XX_BLACK);
  }
  tft.print("." + String(int(10 * (humid - floor(humid)))));

  tft.setCursor(0, 160);
  tft.setTextSize(7);

  if (flag == "GREEN") {
    tft.fillRect(0, 140, 350, 140, ST77XX_GREEN);  //bg
    tft.setTextColor(ST77XX_BLACK); //text
    tft.println(" GREEN ");
  } else if (flag == "RED") {
    tft.fillRect(0, 140, 350, 140, ST77XX_RED);
    tft.setTextColor(ST77XX_WHITE);
    tft.println("  RED  ");
  } else if (flag == "YELLOW") {
    tft.fillRect(0, 140, 350, 140, ST77XX_YELLOW);
    tft.setTextColor(ST77XX_BLACK);
    tft.println(" YELLOW ");
  } else if (flag == "BLACK") {
    tft.fillRect(0, 140, 350, 140, ST77XX_BLACK);
    tft.setTextColor(ST77XX_WHITE);
    tft.println(" BLACK ");
  } else if (flag == "WHITE") {
    tft.fillRect(0, 140, 350, 140, ST77XX_WHITE);
    tft.setTextColor(ST77XX_BLACK);
    tft.println(" WHITE ");
  }

  tft.setCursor(5, 220);
  tft.setTextSize(2);
  tft.println(String(hic, 1));
  tft.setTextSize(1);
  tft.setCursor(55, 217);
  tft.print("o");

  if (status == "online") {
    tft.setCursor(240, 220);
    if (flag == "GREEN") {
      tft.setTextColor(ST77XX_BLUE);
    } else {
      tft.setTextColor(ST77XX_GREEN);
    }
  } else {
    tft.setCursor(230, 220);
    if (flag == "RED") {
      tft.setTextColor(ST77XX_BLACK);
    } else {
      tft.setTextColor(ST77XX_RED);
    }
  }
  if (status == "severfail") {
    tft.setCursor(210, 220);
  }
  tft.setTextSize(2);
  String s = status;
  s.toUpperCase();
  tft.print(s);
}

// --------------------------------------------------
// setup
// --------------------------------------------------
void setup() {
  Serial.begin(115200);

  dht.begin();

  // setup screen
  if (SCREEN_VERSION == 1) {
    tft.init(240, 320);
    tft.invertDisplay(false);
    tft.setRotation(3);
  } else if (SCREEN_VERSION == 2) {
    tft.init(240, 320);
    tft.setRotation(1);
  }

  tft.fillScreen(ST77XX_BLACK);
  tft.setTextColor(ST77XX_RED);
  tft.setCursor(0, 40);
  tft.setTextSize(5);
  tft.println(DEVICE_ID);

  WiFi.mode(WIFI_STA);

  wm.setConfigPortalBlocking(false);
  wm.setConfigPortalTimeout(60);

  if (wm.autoConnect("AutoConnectAP")) {
    Serial.println("connected...yeey :)");
  } else {
    Serial.println("Configportal running");
  }
}

// --------------------------------------------------
// loop
// --------------------------------------------------
void loop() {
  int bat_percentage = analogRead(A0);
  Serial.print("Battery Percent (raw A0): ");
  Serial.println(bat_percentage);

  // ครั้งแรกที่ต่อ WiFi ได้ → ไปดึง config จาก FastAPI
  if (WiFi.status() == WL_CONNECTED && firsttime == true) {
    firsttime = false;
    timeClient.begin();

    // GET CONFIG จาก FastAPI
    read_config_from_api();

    // show config on screen
    tft.fillScreen(ST77XX_BLACK);
    tft.setTextColor(ST77XX_RED);
    tft.setCursor(0, 40);
    tft.setTextSize(5);
    tft.println(DEVICE_ID);

    tft.setTextColor(ST77XX_YELLOW);
    tft.setTextSize(4);
    tft.setCursor(0, 120);
    String s = "";
    if (adjust_temp > 0) {
      s = "+";
    }
    tft.println("Temp " + s + String(adjust_temp));
    tft.setCursor(0, 170);
    s = "";
    if (adjust_humid > 0) {
      s = "+";
    }
    tft.println("Humid " + s + String(adjust_humid));

    delay(5000);
  }

  // read DHT
  humid = dht.readHumidity() + adjust_humid;
  temp  = dht.readTemperature() + adjust_temp;

  if (isnan(humid) || isnan(temp)) {
    humid = -99;
    temp  = -99;
    hic   = -99;
  } else {
    hic = dht.computeHeatIndex(temp, humid, false);
  }

  // คำนวณ flag / water / train / rest
  if (hic == -99) {
    flag  = "none";
    water = -1;
    train = -1;
    rest  = -1;
  } else if (hic < 27) {
    flag  = "WHITE";
    water = 0.5;
    train = 60;
    rest  = 0;
  } else if (hic < 32) {
    flag  = "GREEN";
    water = 1.5;
    train = 50;
    rest  = 10;
  } else if (hic < 41) {
    flag  = "YELLOW";
    water = 1;
    train = 45;
    rest  = 15;
  } else if (hic < 55) {
    flag  = "RED";
    water = 1;
    train = 30;
    rest  = 30;
  } else {
    flag  = "BLACK";
    water = 1;
    train = 20;
    rest  = 40;
  }

  Serial.print("temp: ");        Serial.println(temp);
  Serial.print("humid: ");       Serial.println(humid);
  Serial.print("adjust_temp: "); Serial.println(adjust_temp);
  Serial.print("adjust_humid: ");Serial.println(adjust_humid);

  if (WiFi.status() == WL_CONNECTED) {
    status = "online";
    timeClient.update();
    Serial.print("timeClient.getMinutes(): ");
    Serial.println(timeClient.getMinutes());

    // ส่งเข้า FastAPI ทุก ๆ 10 นาที (นาทีหาร 10 ลงตัว) ใช้ SEND_DATA กันยิงซ้ำ
    if (SEND_DATA == false && timeClient.getMinutes() % 10 == 0) {
      // flag ส่งเข้า API เป็นตัวเล็ก
      String flag_lower = flag;
      flag_lower.toLowerCase();

      int httpResponseCode = post_history_api(DEVICE_ID, temp, humid, hic, flag_lower);
      if (httpResponseCode <= 0 || (httpResponseCode != 200 && httpResponseCode != 201)) {
        status = "severfail";  // ตัวสะกดเดิมในโค้ด :)
      }
      SEND_DATA = true;
    }

    // ถ้านาทีไม่ใช่เลขหาร 10 ลงตัวแล้ว → reset ให้สามารถยิงรอบถัดไปได้
    if (timeClient.getMinutes() % 10 != 0) {
      SEND_DATA = false;
    }
  } else {
    status = "offline";
  }

  // อัปเดตหน้าจอ
  updateScreen(flag, temp, humid, status, bat_percentage);

  wm.process();
  delay(5000);
}
