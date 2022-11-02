# GSASummary

This script generates a HTML report for Google Scholar Alerts by reading your emails from your Mail.app (only for macOS)

## Usage
1. You should change the email address in the script, and also optionally change the email sending method in `send_email()` (by default I use remote iCloud mail server and the password is saved in a file called 'MAIL_PASSWORD' in this directory).
2. Give full disk access permission to your python interpreter.
3. Give execute permission to `run.sh`.
4. Put the following script at `~/Library/LaunchAgents/local.GSASummary.plist`
    ```
   <?xml version="1.0" encoding="UTF-8"?>

   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.   com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key>
     <string>local.GSASummary</string>
     <key>ProgramArguments</key>
     <array>
       <string>{{ SCRIPT_PATH }}/run.sh</string>
     </array>
     <key>StartCalendarInterval</key>
     <dict>
       <key>Hour</key>
       <integer>10</integer>
       <key>Minute</key>
       <integer>00</integer>
     </dict>
     <key>UserName</key>
     <string>lizytalk</string>
     <key>StandardErrorPath</key>
     <string>/usr/local/var/log/GSASummary.log</string>
       <key>StandardOutPath</key>
     <string>/usr/local/var/log/GSASummary.log</string>
   </dict>
   </plist>
    ```
   Remember to change the SCRIPT_PATH.
5. `launchctl load ~/Library/LaunchAgents/local.GSASummary.plist`. Now it's OK. Note that `launchd` can run the job after the computer is awake from sleep while `crontab` will just skip the job if the computer is asleep.