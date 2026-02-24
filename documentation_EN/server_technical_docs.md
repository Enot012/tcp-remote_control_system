# 📚 Server Documentation - Technical Structure

## 🏗️ Architecture

### Global Data Structures

```python
clients: Dict[str, asyncio.StreamWriter]
```
**Purpose:** Stores active client connections  
**Key:** `client_id` (client name)  
**Value:** `StreamWriter` for sending data  
**Usage:** Sending commands, checking online status

```python
output_buffers: Dict[str, Dict[str, Any]]
```
**Purpose:** Temporary accumulation of results in parts  
**Structure:**
```python
{
    "client_id": {
        "type": "OUTPUT" | "FILETRU",  # Data type
        "lines": [...],                # Accumulated lines
        "chunks": int,                 # Number of received chunks
        "total": int                   # Expected total count
    }
}
```
**Usage:** Assembling results from `OUTPUT:CHUNK:` messages

```python
last_outputs: Dict[str, Dict[str, Any]]
```
**Purpose:** Stores the last complete result from each client  
**Structure:**
```python
{
    "client_id": {
        "content": str,      # Full output
        "type": str,         # Command type
        "timestamp": str     # Time received
    }
}
```
**Usage:** The `save` command for saving results

```python
active_commands: Dict[str, Dict[str, Any]]
```
**Purpose:** Tracks currently executing commands  
**Structure:**
```python
{
    "client_id": {
        "start_time": float,           # Start time
        "command": str,                # Command text
        "type": str,                   # CMD/FILETRU/IMPORT/EXPORT
        "total_commands": int,         # For SIMPL: total command count
        "received_commands": int,      # For SIMPL: received count
        "accumulated_output": [str]    # For SIMPL: accumulated results
    }
}
```
**Usage:** Accumulating SIMPL results, timeouts, saving

```python
scheduled_tracking: Dict[str, list]
```
**Purpose:** Tracks currently executing scheduled commands  
**Structure:**
```python
{
    "client_id": [0, 2, 5]  # Command indices from scheduled_commands.json
}
```
**Usage:** Marking scheduled commands as completed

---

## 📦 Classes

### `Config`
**Purpose:** Centralized server configuration

**Constants:**
```python
BASE_DIR = ".../HA_12"
DIR_SAVE, DIR_TRASH, DIR_HISTORY, DIR_FILES, DIR_LOGS, DIR_JSON
FILE_CODE, FILE_USERS, FILE_STATE, FILE_CRASH_LOG
FILE_GROUPS, FILE_SCHEDULED
HOST = "0.0.0.0"
PORT = 9000
CHUNK_SIZE = 65536
COMMAND_TIMEOUT = 120      # Warning for long-running command
WARNING_TIMEOUT = 90       # First warning
READ_TIMEOUT = 300         # Read timeout (client disconnect)
STATE_SAVE_INTERVAL = 30   # State save interval
TIMEZONE_OFFSET = +1       # Timezone
```

---

### `Logger`
**Purpose:** Centralized logging system

**Methods:**
```python
@staticmethod
def log(level: str, message: str, client_id: Optional[str] = None, 
        show_console: bool = True)
```
- Logs events to file and console
- Levels: INFO, ERROR, CONNECT, DISCONNECT, CMD_START, KICK, etc.
- Format: `[2025-02-23 10:30:00] [INFO] [client_id] message`

---

### `UserManager`
**Purpose:** User management

**Methods:**

```python
@staticmethod
def load_users() -> Dict
```
Loads `users.json` with user information

```python
@staticmethod
def register(client_id: str) -> str
```
Registers a new connection and returns the alias

```python
@staticmethod
def get_by_alias(alias: str) -> Optional[str]
```
Returns `client_id` by alias

```python
@staticmethod
def update_status(client_id: str, status: str)
```
Updates status (ON/OFF) and timestamp

**users.json structure:**
```json
{
  "users": {
    "enot": {
      "alias": "SuperUser",
      "status": "ON",
      "first_login": "2025-02-23 10:00:00",
      "last_login": "2025-02-23 10:30:00",
      "last_logout": "2025-02-22 18:00:00"
    }
  }
}
```

---

### `StateManager`
**Purpose:** Server state persistence

**Methods:**

```python
@staticmethod
def save()
```
Saves the current state to `server_state.json`:
```json
{
  "online_users": ["enot", "user2"],
  "active_commands_count": 3,
  "last_save": "2025-02-23 10:30:00"
}
```

---

### `BanManager`
**Purpose:** Client disconnection management

**Methods:**

```python
@staticmethod
async def kick_user(username: str, reason: str = "Disconnected by administrator")
```
- Sends `KICK:reason` to the client
- Closes the connection
- Logs the action

---

### `GroupManager`
**Purpose:** User group management

**Methods:**

```python
@staticmethod
def load_groups() -> Dict[str, list]
```
Loads `groups.json`

```python
@staticmethod
def save_groups(groups: Dict[str, list]) -> bool
```
Saves groups

```python
@staticmethod
def get_group_members(name: str) -> Optional[list]
```
Returns the list of members in a group

**groups.json structure:**
```json
{
  "admins": ["user1", "user2"],
  "developers": ["user3", "user4", "user5"]
}
```

---

### `ScheduledCommandManager`
**Purpose:** Scheduled command management

**Methods:**

```python
@staticmethod
def load_data() -> Dict
```
Loads `scheduled_commands.json`

```python
@staticmethod
def save_data(data: Dict) -> bool
```
Saves scheduled commands

```python
@staticmethod
def get_expected_users(target: str) -> list
```
Builds the user list for a given target:
- `all` → all users from `users.json`
- `group:name` → group members
- `username` → single user

```python
@staticmethod
def get_commands_for_user(username: str) -> list
```
Returns pending commands for a user

```python
@staticmethod
def mark_completed(cmd_index: int, username: str, output: str)
```
Marks a command as completed:
- Moves the user from `expected_users` → `completed_users`
- Writes the result to a file
- If `expected_users` is empty → moves to `completed`

```python
@staticmethod
def write_output(target: str, username: str, output: str)
```
Writes the result to a file:
- `all` → `ALL.txt`
- `group:name` → `group_name.txt`
- `username` → `username.txt`

```python
@staticmethod
def replace_user_placeholder(text: str, username: str) -> str
```
Replaces `{User}` with the username

**scheduled_commands.json structure:**
```json
{
  "commands": [
    {
      "target": "all",
      "command_type": "CMD",
      "command": "whoami",
      "created_at": "2025-02-23 10:00:00",
      "expected_users": ["user1", "user2"],
      "completed_users": []
    }
  ],
  "completed": [
    {
      "target": "group:admins",
      "command_type": "SIMPL",
      "created_at": "2025-02-22 18:00:00",
      "completed_at": "2025-02-23 09:00:00",
      "expected_users": [],
      "completed_users": ["admin1", "admin2"]
    }
  ]
}
```

---

### `CommandMonitor`
**Purpose:** Command execution monitoring

**Methods:**

```python
@staticmethod
def register(client_id: str, command: str, cmd_type: str, cmd_count: int)
```
Registers the start of command execution in `active_commands`

```python
@staticmethod
def save_command_output(client_id: str, command: str, output: str, cmd_type: str)
```
Saves the result to a file:
- Retrieves the user's alias
- Saves to `~/trash/output_command_{alias}.txt`
- Format: timestamp, command, type, output

```python
@staticmethod
def unregister(client_id: str)
```
Removes the command from `active_commands` after completion

---

### `FileTransfer`
**Purpose:** File transfer between server and clients

**Methods:**

```python
@staticmethod
async def import_to_client(client_id: str, source_path: str, dest_dir: str)
```
Sends files to the client:
1. Scans the path (file or directory)
2. Sends `IMPORT:START:{count}`
3. For each file: metadata + 64KB chunks
4. Sends `IMPORT:END`

```python
@staticmethod
async def send_file(writer: StreamWriter, file_path: Path, rel_path: str)
```
Sends a single file:
- Metadata: name, size, relative path
- Content in 64KB chunks

```python
@staticmethod
async def receive_file(reader: StreamReader, save_path: Path, file_size: int) -> bool
```
Receives a file from the client:
- Creates directories
- Reads in 64KB chunks
- Verifies size

---

## 🔄 Core Functions

### `async def handle_client(reader, writer)`
**Purpose:** Main handler for client connections

**Process:**
1. **Connection:** Receives `client_id`, registers the client
2. **Scheduled commands:** Automatically executes pending commands on connect
3. **Main loop:** Processes incoming messages
   - `EXPORT:START:` → receive files
   - `OUTPUT:START:` → start of result
   - `OUTPUT:CHUNK:` → accumulate result
   - `OUTPUT:END` → finalize, save
   - `FILETRU:START:` → start of SIMPL result
4. **Disconnection:** Updates status, logs event

**Fault tolerance:**
- Read timeouts (300 sec)
- try-except blocks for error isolation
- Consecutive error counter
- Graceful degradation

---

### `async def server_input(server)`
**Purpose:** Processes administrator commands

**Supported commands:**

#### Command Execution
- `CMD <client|all> <command>` - execute a command
- `simpl <client|all>` - execute commands from `code.txt`
- `cancel <client>` - cancel execution

#### Files
- `import <client|all> <server_path> [client_path]` - send a file
- `export <client> <client_path> [optional]` - receive a file
- `save <client> <name>` - save the last result

#### Management
- `list` - list users
- `status` - active commands
- `kick <client|all>` - disconnect

#### Scheduled Commands
- `chart_new` - create a scheduled command
- `chart_list` - show active scheduled commands
- `chart_comd` - show completed scheduled commands
- `chart_del <index>` - delete a scheduled command

#### Groups
- `group_new <n>` - create a group
- `group_list` - show groups
- `group_del <n>` - delete a group

---

## 📡 Communication Protocol

### Server → Client

```python
CMD:{command}\n                    # Execute command
FILETRU:{command}\n                # Execute command (from SIMPL)
EXPORT;{source};{dest}\n           # Request files
IMPORT:START:{count}\n             # Start of file transfer
FILE:META:{json}\n                 # File metadata
FILE:CHUNK:{size}\n{data}          # File chunk
IMPORT:END\n                       # End of transfer
KICK:{reason}\n                    # Disconnect
```

### Client → Server

```python
OUTPUT:START:{count}\n             # Start of result
OUTPUT:CHUNK:{escaped_data}\n      # Result chunk
OUTPUT:END\n                       # End of result
EXPORT:START:{json}\n              # Start of file upload
FILE:META:{json}\n                 # File metadata
FILE:CHUNK:{size}\n{data}          # File chunk
EXPORT:COMPLETE\n                  # End of transfer
```

---

## 🔧 Implementation Details

### Result Chunking
**Problem:** Large outputs may be truncated  
**Solution:** Split into chunks, replacing `\n` with `<<<NL>>>`

```python
# Client
for i in range(0, len(output), chunk_size):
    chunk = output[i:i+chunk_size]
    escaped = chunk.replace('\n', '<<<NL>>>')
    send(f"OUTPUT:CHUNK:{escaped}\n")

# Server
chunk_restored = chunk_data.replace('<<<NL>>>', '\n')
output_buffers[client_id]["lines"].append(chunk_restored)
```

### SIMPL Result Accumulation
**Problem:** SIMPL sends N commands → N results  
**Solution:** Accumulate in `accumulated_output`

```python
# On each OUTPUT:END
cmd_info["accumulated_output"].append(full_output)
cmd_info["received_commands"] += 1

# When all results received
if received >= total:
    combined = "\n\n".join(accumulated_output)
    save_command_output(client_id, command, combined, "OUTPUT")
```

### Alias Usage in Files
**Why:** Human-readable filenames instead of client_id

```python
# Get alias
with open(Config.FILE_USERS, "r", encoding="utf-8") as f:
    data = json.load(f)
    alias = data['users'][client_id]["alias"]

# Usage
filename = f"output_command_{alias}.txt"
client_dir = Path(Config.DIR_FILES) / alias
```

---

## 📊 Data Flow Diagram

```
┌─────────────┐
│  Admin      │
│  enters     │
│  command    │
└──────┬──────┘
       ↓
┌──────────────────────────────────────┐
│  server_input()                      │
│  Parse command                       │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  CommandMonitor.register()           │
│  active_commands[client] = {...}     │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  clients[client_id].write()          │
│  Send command to client              │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  handle_client()                     │
│  Waiting for result                  │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  OUTPUT:CHUNK: → output_buffers      │
│  Accumulating in parts               │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  OUTPUT:END                          │
│  accumulated_output.append()         │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  All results received?               │
│  received >= total                   │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  CommandMonitor.save_command_output()│
│  ~/trash/output_command_{alias}.txt  │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  last_outputs[client] = {...}        │
│  Saved for the save command          │
└──────────────────────────────────────┘
```

---

## 🎯 Key Points

1. **Asynchronous:** `asyncio` allows handling multiple clients simultaneously
2. **Result accumulation:** For SIMPL, all results are gathered before saving
3. **Chunking:** Large data is transferred in parts for reliability
4. **Scheduled commands:** Automatically executed upon client connection
5. **Groups:** Bulk command delivery to user groups
6. **Aliases:** Convenient usernames for file naming
7. **Fault tolerance:** Error isolation, timeouts, graceful degradation
8. **Logging:** Complete history of all actions

---

**Version:** 3.0  
**Last updated:** 2025-02-23
