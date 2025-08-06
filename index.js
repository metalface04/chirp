const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const twilio = require('twilio');
const { SarvamAIClient } = require("sarvamai");

const app = express();
const port = 3000;
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.post('/voice', (req, res) => {
    const ngrokUrl = 'fc852a016f02.ngrok-free.app'; // replace every ngrok restart
    const twiml = new twilio.twiml.VoiceResponse();

    twiml.say('The output is on Chirag’s laptop');

    const connect = twiml.connect();
    connect.stream({
        url: `wss://${ngrokUrl}/audio`
    });

    twiml.say('The audio stream is now active.');
    twiml.pause({ length: 10 });

    res.type('text/xml');
    res.send(twiml.toString());
});

wss.on('connection', async (ws) => {
    console.log('📞 WebSocket connection established with Twilio!');

    const client = new SarvamAIClient({
        apiSubscriptionKey: "sk_d0foygdr_e2WpBOEombSQ1kILYxXpEXTW"
    });

    let sarvamSocket;
    let sarvamReady = false;

    try {
        sarvamSocket = await client.speechToTextStreaming.connect({
            "language-code": "kn-IN"
        });
        sarvamReady = true;
        console.log("✅ Sarvam.ai WebSocket is ready.");

        sarvamSocket.on("message", (message) => {
            if (message.type === "final") {
                console.log("✅ Final Transcription:", message.data.text);
            } else if (message.type === "partial") {
                console.log("📝 Partial Transcription:", message.data.text);
            } else {
                console.log("📦 Other Message:", message);
            }
        });

        sarvamSocket.on("error", (err) => {
            console.error("❌ Sarvam.ai socket error:", err);
        });

        sarvamSocket.on("close", () => {
            console.warn("⚠️ Sarvam.ai socket closed.");
            sarvamReady = false;
        });

    } catch (err) {
        console.error("❌ Failed to connect to Sarvam.ai:", err);
    }

    ws.on('message', async (message) => {
        try {
            const msg = JSON.parse(message);
            if (msg.event === 'media') {
                const audioChunk = Buffer.from(msg.media.payload, 'base64');
                const base64Audio = audioChunk.toString('base64');

                if (sarvamReady) {
                    sarvamSocket.transcribe({
                        "audio.data": base64Audio,
                        "audio.encoding": "audio/mulaw",
                        "audio.sample_rate": 8000
                    });
                } else {
                    console.warn("🚫 Tried to transcribe but Sarvam.ai socket is not open.");
                }
            }
        } catch (err) {
            console.error("💥 Error processing audio message:", err);
        }
    });

    ws.on('close', () => {
        console.log('📴 Twilio WebSocket closed.');
        if (sarvamSocket) sarvamSocket.close();
    });
});

server.listen(port, () => {
    console.log(`🚀 Server and WebSocket running on http://localhost:${port}`);
});
