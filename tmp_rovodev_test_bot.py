#!/usr/bin/env python3
"""
Test script for the Instagram ceramics studio chatbot
Simulates Instagram webhook messages to test the bot locally
"""

import requests
import json

# Test the bot locally
BASE_URL = "http://localhost:5000"

def test_webhook_verification():
    """Test webhook verification (GET request)"""
    print("Testing webhook verification...")
    params = {
        'hub.verify_token': 'gnfbtGY^%&RTYghjui',
        'hub.challenge': 'test_challenge_123'
    }
    
    try:
        response = requests.get(f"{BASE_URL}/webhook", params=params)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Bot is not running. Please start the bot first with: python ig_chat_bot.py")
        return False

def test_message_handling(message_text):
    """Test message handling (POST request)"""
    print(f"\nTesting message: '{message_text}'")
    
    # Simulate Instagram webhook payload
    payload = {
        "object": "instagram",
        "entry": [{
            "messaging": [{
                "sender": {"id": "test_user_123"},
                "message": {"text": message_text}
            }]
        }]
    }
    
    try:
        response = requests.post(f"{BASE_URL}/webhook", json=payload)
        print(f"Status: {response.status_code}")
        print(f"Response: {response.text}")
        return response.status_code == 200
    except requests.exceptions.ConnectionError:
        print("❌ Bot is not running. Please start the bot first with: python ig_chat_bot.py")
        return False

def main():
    print("🧪 Testing Instagram Ceramics Studio Chatbot")
    print("=" * 50)
    
    # Test webhook verification
    if not test_webhook_verification():
        return
    
    print("\n✅ Webhook verification successful!")
    
    # Test various messages
    test_messages = [
        "Cześć! Jakie są ceny warsztatów?",
        "Kiedy jesteście otwarci?",
        "Gdzie się znajdujecie?",
        "Chciałbym zarezerwować warsztat dla 4 osób",
        "Привіт! Скільки коштують майстер-класи?",
        "rezerwacja na przyszły tydzień"
    ]
    
    print("\n📱 Testing message handling...")
    for message in test_messages:
        test_message_handling(message)
    
    print("\n✅ Testing completed!")
    print("\nNote: The bot will try to send responses via Instagram API.")
    print("Check the console output of the running bot for AI responses.")

if __name__ == "__main__":
    main()