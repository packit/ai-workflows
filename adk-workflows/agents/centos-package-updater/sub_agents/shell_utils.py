"""
Shell command execution utilities for ADK agents.
"""
import subprocess

def shell_command(command: str) -> str:
    """
    Execute a shell command using subprocess and return the output.

    Args:
        command: The shell command to execute

    Returns:
        String containing the command output and any errors
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            # TODO: change this to avoid shell injection
            shell=True,
            timeout=300  # 5 minute timeout
        )

        output_parts = []
        output_parts.append(f"Command: {command}")
        output_parts.append(f"Return code: {result.returncode}")

        # Always include stdout section, even if empty
        if result.stdout:
            output_parts.append(f"STDOUT:\n{result.stdout}")
        else:
            output_parts.append("STDOUT: (no output)")

        # Always include stderr section, even if empty
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        else:
            output_parts.append("STDERR: (no errors)")

        # Add execution status summary
        if result.returncode == 0:
            if result.stdout.strip():
                output_parts.append("STATUS: Command executed successfully with output")
            else:
                output_parts.append("STATUS: Command executed successfully (no output produced)")
        else:
            output_parts.append(f"STATUS: Command failed with exit code {result.returncode}")

        return "\n".join(output_parts)

    except subprocess.TimeoutExpired:
        return f"Command: {command}\nReturn code: TIMEOUT\nSTDOUT: (no output)\nSTDERR: (no errors)\nSTATUS: Command timed out after 300 seconds"
    except Exception as e:
        return f"Command: {command}\nReturn code: ERROR\nSTDOUT: (no output)\nSTDERR: {str(e)}\nSTATUS: Command execution failed"
