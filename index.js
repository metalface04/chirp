const express = require('express');
const http = require('http');
const WebSocket = require('ws');
const twilio = require('twilio');
const fs = require('fs');
const ffmpeg = require('fluent-ffmpeg');

const app = express();
const port = 3000;
const server = http.createServer(app);
const wss = new WebSocket.Server({ server });

app.post('/voice', (req, res) => {
    const ngrokUrl = '3ce393ff95f9.ngrok-free.app'; // Replace this
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
        length: 14  // 4 hours
    });

    // Send the complete TwiML to Twilio
    res.type('text/xml');
    res.send(twiml.toString());
});

wss.on('connection', (ws) => {
    console.log('WebSocket connection established!');
    
    const rawAudioFile = 'output.raw';
    const writeStream = fs.createWriteStream(rawAudioFile);

    ws.on('message', (message) => {
        const msg = JSON.parse(message);
        if (msg.event === 'media') {
            const audioChunk = Buffer.from(msg.media.payload, 'base64');
            writeStream.write(audioChunk);
        }
    });

    ws.on('close', () => {
        console.log('WebSocket connection closed. Finalizing audio file...');
        writeStream.end();

        ffmpeg()
            .input(rawAudioFile)
            .inputFormat('mulaw')
            .inputOptions(['-ar 8000', '-ac 1'])
            .output('output.wav')
            .on('end', () => {
                console.log('Conversion to WAV finished!');
                fs.unlinkSync(rawAudioFile);
            })
            .on('error', (err) => {
                console.error('Error during conversion:', err.message);
            })
            .run();
    });
});

// Start the server only ONCE using the http server instance
server.listen(port, () => {
    console.log(`Server and WebSocket running on http://localhost:${port}`);
});