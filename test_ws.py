import asyncio
import websockets

async def test():
    uri = "ws://localhost:8090/v1/chatbot/ws/test1234?brand_id=sutracode"
    try:
        async with websockets.connect(uri) as websocket:
            print("Connected!")
            await websocket.close()
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(test())
