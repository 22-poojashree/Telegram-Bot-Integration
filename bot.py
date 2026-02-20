from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
import requests
from datetime import date, timedelta
from openai import OpenAI
import re
import dateparser
from dateparser.search import search_dates
import json

from config import (
    TELEGRAM_BOT_TOKEN,
    FRAPPE_URL,
    API_KEY,
    API_SECRET,
)

class TelegramLeaveBot:
    def __init__(self):
        self.telegram_token=TELEGRAM_BOT_TOKEN
        self.frappe_api=FrappeAPI(FRAPPE_URL,API_KEY,API_SECRET)
        self.load_settings()
        
    def load_settings(self):
        settings=self.frappe_api.get_bot_settings()
        if not settings:
            return 
        
        self.parsing_mode=settings["parsing_mode"]
        if self.parsing_mode=="By AI API":
            self.deepseek_api=settings["api_key"]
            self.model_name=settings["ai_model_name"]
        elif self.parsing_mode=="By local model":
            self.ollama_url=settings["ollama_url"]
            self.local_model_name=settings["local_model_name"]
        
        
    async def handle_message(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        message=update.message
        if not message.entities:
            return
        bot_mentioned=False
        for entity in message.entities:
            if entity.type=="mention":
                mention_text=message.text[entity.offset:entity.offset+entity.length]
                if mention_text=="@apply_leave_bot":
                    bot_mentioned=True
                    break
        if not bot_mentioned:
            return
        if not message.from_user.username:
            await message.reply_text("Please set a Telegram username in your profile to use the bot")
            return
        telegram_username=message.from_user.username
        user=self.frappe_api.get_user_by_telegram_username(telegram_username)
        
        if not user:
            await message.reply_text("your telegram username is not linked , please contact admin")
            return
        self.load_settings()
        leave_info=self.parse_leave_request(message.text)
        print("Leave Info:",leave_info)
        
        if not leave_info:
            await message.reply_text("I couldn't understand your leave request.")
            return
        
        if leave_info["leave_type"]!="Work From Home":
            result=self.frappe_api.create_leave_application_draft(
                employee_name=user["full_name"],
                from_date=leave_info["from_date"],
                to_date=leave_info["to_date"],
                reason=leave_info["reason"],
                half_day=leave_info["half_day"],
                leave_type="Half Day" if leave_info.get("half_day") else "Full Day",
            )
            if result["success"]:
                await message.reply_text("Leave application draft created.")
            else:
                await message.reply_text("failed to create leave application.")
                
        else:
            result=self.frappe_api.create_attendance_request_draft(
                employee_name=user["email"],
                from_date=leave_info["from_date"],
                to_date=leave_info["to_date"],
                reason=leave_info["reason"],
                half_day=leave_info["half_day"],
                leave_type="Work From Home",
            )
            if result["success"]:
                await message.reply_text("Attendance request draft created.")
            else:
                await message.reply_text("failed to create attendance request.")
            
            
    async def handle_mark_attendance(self,update:Update,context:ContextTypes.DEFAULT_TYPE):
        command=update.message
        
        is_mark_cmd=False
        for entity in command.entities:
            if entity.type=="bot_command" and command.text.startswith("/mark_attendance"):
                is_mark_cmd=True
                break
        if not is_mark_cmd:
            return
        
        user=command.from_user.username
        if user not in ["admin","Poojashree_Ravi"]:
            await command.reply_text("You are not admin.")
            return 
        self.frappe_api.mark_attendance_for_today()
        await command.reply_text("attendance marked for today.")
        
    def parse_leave_request(self,text):
        print("Parsing mode:",self.parsing_mode)
        if self.parsing_mode=="By Rules":
            return self.parse_by_rules(text)
        elif self.parsing_mode=="By AI API":
            return self.parse_by_deepseek(text)
        elif self.parsing_mode=="By local model":
            return self.parse_by_local_ai(text)
        else:
            return None
        
    def parse_by_rules(self,text):
        print("By RULES")
        text_lower=text.lower()
        actual_text=text_lower.replace("@apply_leave_bot","").strip()
        # print("actual_text:\n", actual_text)
        leave=0
        work_from_home=0
        leave_keywords=["leave","day off","absent","off","not available","vacation","holiday"]
        work_from_home_keywords=["work from home","wfh","remote work","work remotely","working from home"]
        
        if not any(k in actual_text for k in leave_keywords):
            if not any(k in actual_text for k in work_from_home_keywords):
                return None
            else:
                work_from_home=1
        else:
            leave=1
        
        half_day=0
        if any(k in actual_text for k in ["second half","2nd half","afternoon","post lunch","first half","1st half","morning","pre lunch"]):
            half_day=1
        
        today=date.today()
        from_date=None
        to_date=None
        
        range_patterns=[
            r'from\s+(.+?)\s+to\s+(.+?)(?:\s|$|\.)',
            r'(\d{1,2}[-/]\d{1,2}[-/]\d{4})\s+to\s+(\d{1,2}[-/]\d{1,2}[-/]\d{4})',
            r'\btoday\b',
            r'next\s+(\w+|\d+)\s+days?',
            r'next\s+week\s+from\s+today',
            r'\bthis\s+week\b',
        ]
        
        for pattern in range_patterns:
            match=re.search(pattern,actual_text)
            if not match:
                continue
            
            if pattern.startswith("from"):
                from_date=dateparser.parse(match.group(1))
                to_date=dateparser.parse(match.group(2))
            elif "[-/]" in pattern:
                from_date=dateparser.parse(match.group(1))
                to_date=dateparser.parse(match.group(2))
            elif "days" in pattern:
                value=match.group(1)
                word_number={
                   "one":1,"two":2,"three":3,
                    "four":4,"five":5,"six":6,
                    "seven":7,"eight":8,"nine":9,
                    "ten":10 
                }
                if value.isdigit():
                    days=int(value)
                else:
                    days=word_number.get(value.lower(),1)
                    
                from_date=today+timedelta(days=1)
                to_date=from_date+timedelta(days=days-1)
            
            elif "next week" in pattern:
                from_date=today
                to_date=today+timedelta(days=5)
            break
        
        if not from_date:
            date_results=search_dates(text)
            if date_results:
                from_date=date_results[0][1].date()
                to_date=from_date
        if not from_date:
            from_date=today
            to_date=today
        if half_day:
            to_date=from_date

        reason = actual_text.strip()
        
        result={
               "from_date": from_date.strftime('%Y-%m-%d'),
                "to_date": to_date.strftime('%Y-%m-%d'),
                "reason": reason,
                "leave_type":None,
                "half_day": half_day
            }

        if leave:
            result["leave_type"]="Half Day" if half_day else "Full Day"
        if work_from_home:
            result["leave_type"]="Work From Home"
            
        return result
        
        
    def parse_by_deepseek(self,text):
        print("BY AI API")
        client=OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=self.deepseek_api
        )
        
        today=date.today().isoformat()
        prompt=f"""
            Today date is {today}.
            convert relative dates like:
            -tomorrow
            -day after tomorrow
            -next 2 days
            -one week
            -next monday
            into exact calendar dates.
            
            Extract leave details from:"{text}"
            return only valid JSON:
            {{
                "from_date":"YYYY-MM-DD",
                "to_date":"YYYY-MM-DD",
                "total_days":number,
                "reason":"short reason",
                "leave_type":"Full Day/Half Day/Work From Home",
                "half_day":0
            }}
        """
        response=client.chat.completions.create(
            model=self.model_name,
            messages=[{"role":"user","content":prompt}]
        )
        content=response.choices[0].message.content.strip()
        
        if content.startswith("```"):
            content = content.replace("```json", "").replace("```", "").strip()

        return json.loads(content)
    
    def parse_by_local_ai(self,text):
        today=date.today().isoformat()
        prompt=f"""
            Today date is {today}.
            convert relative dates like:
            -tomorrow
            -day after tomorrow
            -next 2 days
            -one week
            -next monday
            into exact calendar dates.
            
            Extract leave details from:"{text}"
            return only valid JSON:
            {{
                "from_date":"YYYY-MM-DD",
                "to_date":"YYYY-MM-DD",
                "total_days":number,
                "reason":"short reason",
                "leave_type":"Full Day/Half Day/Work From Home",
                "half_day":number
            }}
        """
        
        response=requests.post(
            f"{self.ollama_url}/api/generate",
            json={
                "model":self.local_model_name,
                "prompt":prompt,
                "stream":False,
            },
        )
        if response.status_code!=200:
            return None
        result=response.json()
        content=result.get("response","")
        return json.loads(content)
        
    def run(self):
        application=Application.builder().token(self.telegram_token).build()
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        application.add_handler(CommandHandler("mark_attendance", self.handle_mark_attendance))
        print("BOT started....")
        application.run_polling()
        
        
class FrappeAPI:
    def __init__(self,frappe_url,api_key,api_secret):
        self.frappe_url=frappe_url
        self.headers={
            "Authorization": f"token {api_key}:{api_secret}",
            "Content-Type":"application/json"
        }
        
    def get_bot_settings(self):
        url=f"{self.frappe_url}/api/resource/Telegram Bot Settings/Telegram Bot Settings"
        
        response=requests.get(
            url,
            headers=self.headers
        )
        # print("Status code",response.status_code)
        # print("\nRespone",response.text)
        
        if response.status_code!=200:
            print("Failed to fetch setting details")
            return {}
        
        return response.json().get("data",{})
    
    def get_user_by_telegram_username(self,telegram_username):
        telegram_username=telegram_username.lstrip("@")
        url=f"{self.frappe_url}/api/resource/User"
        filters=json.dumps([["telegram_username","=",telegram_username]])
        fields=json.dumps(["username","full_name","email"])
        
        response=requests.get(
            url,
            headers=self.headers,
            params={"filters":filters,"fields":fields,"limit":1}
        )
        data=response.json()
        print("\nData",data)
        if data.get("data"):
            return data["data"][0]
        
        return None
    
    def create_leave_application_draft(self,employee_name,from_date,to_date,reason,half_day,leave_type):
        url=f"{self.frappe_url}/api/resource/Leave Application"
        
        payloads={
            "employee_name":employee_name,
            "from_date":from_date,
            "to_date":to_date,
            "leave_type":leave_type,
            "reason":reason,
            "half_day":half_day,
            "posting_date": date.today().isoformat(),
            "docstatus":0
        }
        
        response=requests.post(url,headers=self.headers,json=payloads)
        doc=response.json()
    
        return{
            "success":response.status_code==200,
            "doc_name":doc.get("data",{}).get("name"),
            "error":doc.get("message")
        }
    
    def create_attendance_request_draft(self,employee_name,from_date,to_date,reason,half_day,leave_type):
        url=f"{self.frappe_url}/api/resource/Attendance Request"
        payloads={
            "employee":employee_name,
            "from_date":from_date,
            "to_date":to_date,
            "reason":reason,
            "type":leave_type,
            "half_day":half_day,
            "posting_date": date.today().isoformat(),
            "docstatus":0
        }
        
        response=requests.post(
            url,
            headers=self.headers,
            json=payloads
        )
        doc=response.json()
    
        return{
            "success":response.status_code==200,
            "doc_name":doc.get("data",{}).get("name"),
            "error":doc.get("message")
        }
        
        
    def mark_attendance_for_today(self):
        today=date.today().isoformat()
        
        employees=self.get_all_employees()
        print("Employees:",employees)
        for employee in employees:
            print("mark attendance for employee:",employee["name"])
            self.mark_attendance(employee["name"],today)
        
    def get_all_employees(self):
        url=f"{self.frappe_url}/api/resource/User"
        response=requests.get(
            url,
            headers=self.headers,
            params={"fields":json.dumps(["name","username"])}
        )
        print("employee status code",response.status_code)
        print("employee response",response.text)
        data=response.json()
        if data.get("data"):
            return data["data"]
        return []
    
    def mark_attendance(self,employee_name,date):
        url=f"{self.frappe_url}/api/resource/Attendance"
        payloads={
                    "employee":employee_name,
                    "status":"Present",
                    "attendance_date":date,
                }
        status=self.check_leave_application(employee_name,date)
        
        if not status:
            request=self.check_attendance_request(employee_name,date)
            if request:
                payloads["status"]=request["type"]
        else:
            payloads["status"]=status["leave_type"]
            
        response=requests.post(
            url,
            headers=self.headers,
            json=payloads
        )
        print("Attendance Payload",payloads)
        print("Status Code",response.status_code)
        print("Response",response.text)
        
    def check_leave_application(self,employee_name,date):
        url=f"{self.frappe_url}/api/resource/Leave Application"
        filters=json.dumps([
            ["employee","=",employee_name],
            ["from_date","<=",date],
            ["to_date",">=",date]
        ])
        fields=json.dumps(["name","leave_type"])
        
        response=requests.get(
            url,
            headers=self.headers,
            params={"filters":filters,"fields":fields,"limit":1}
        )
        if response.status_code!=200:
            return None
        data=response.json().get("data")
        if not data:
            return None
        return {
            "name":data[0]["name"],
            "leave_type":data[0]["leave_type"],
        }
        
    def check_attendance_request(self,employee_name,date):
        url=f"{self.frappe_url}/api/resource/Attendance Request"
        filters=json.dumps([
            ["employee","=",employee_name],
            ["from_date","<=",date],
            ["to_date",">=",date]
        ])
        fields=json.dumps(["type","name"])
        response=requests.get(
            url,
            headers=self.headers,
            params={"filters":filters,"fields":fields,"limit":1}
        )       
        if response.status_code!=200:
            return None
        data=response.json().get("data")
        if not data:
            return None
        return{
            "type":data[0]["type"],
            "name":data[0]["name"]
        }
        
        
def start_bot():
    bot=TelegramLeaveBot()
    bot.run()
        
if __name__=="__main__":
    start_bot()


