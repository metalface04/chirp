const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const twilio = require('twilio');
const { SarvamAI, SarvamAIClient } = require("sarvamai");

const app = express(); // express webapp for handling requests and responses
const port = 3000;
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.post('/voice', (req, res) => {
    const ngrokUrl = '3ce393ff95f9.ngrok-free.app'; // Replace this with new url everytime we start ngrok
    const twiml = new twilio.twiml.VoiceResponse();
    twiml.say('The output is on Chirags laptop');
    // 1. Initiate the connection and start the stream.
    // This action happens in the background.
    const connect = twiml.connect();
    connect.stream({
        url: `wss://${ngrokUrl}/audio`
    });

    // 2. Say a message to the caller. This happens AFTER the stream is initiated.
    twiml.say('The audio stream is now active.');

    // 3. Pause the main call thread to keep the call alive.
    // The stream, running in the background, will continue for this duration.
    twiml.pause({
        length: 10 // 10 seconds
    });

    // Send the complete TwiML to Twilio
    res.type('text/xml');
    res.send(twiml.toString());
});

wss.on('connection', async (ws) => {
    console.log('WebSocket connection established with Twilio!');

    // Init SarvamAI client
    const client = new SarvamAIClient({
        apiSubscriptionKey: process.env.SARVAM_API_KEY
    });
    const sarvamSocket = await client.speechToTextStreaming.connect({
        "language-code": "en-IN",  // Change to kn-IN or others as needed
        "model": "saarika:v2.5",
        "high-vad-sensitivity": true
    });

    // Wait for Sarvam socket to open
    await sarvamSocket.waitForOpen();
    console.log("Sarvam.ai WebSocket is ready.");

    sarvamSocket.on("message", (message) => {
        console.log("📝 Transcription:", message);
    });

    sarvamSocket.on("error", (err) => {
        console.error("Sarvam.ai socket error:", err);
    });

    sarvamSocket.on("close", () => {
        console.log("Sarvam.ai connection closed.");
    });

    ws.on('message', async (message) => {
        try {
            const msg = JSON.parse(message);
            if (msg.event === 'media') {
                const audioChunk = Buffer.from(msg.media.payload, 'base64');
                
                const audioData = {
                    data: audioChunk.toString('base64'),  // required to be base64-encoded
                    sample_rate: 8000,  // Twilio streams at 8000 Hz
                    encoding: "audio/mulaw"
                };

                sarvamSocket.transcribe({ audio: audioData });
            }
        } catch (err) {
            console.error("Error processing audio message:", err);
        }
    });

    ws.on('close', () => {
        console.log('Twilio WebSocket closed.');
        sarvamSocket.close();
    });
});

// Start the server only ONCE using the http server instance
server.listen(port, () => {
    console.log(`Server and WebSocket running on http://localhost:${port}`);
});