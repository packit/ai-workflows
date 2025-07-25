#!/usr/bin/env python3
"""
Simple Container Workflow using Llama Stack's native session storage
No Redis needed - uses Llama Stack sessions for agent coordination
"""

import sys
import os
import yaml
import json
import time
from pprint import pprint
from llama_stack_client import LlamaStackClient, Agent

def load_agent_config(config_name):
    """Load agent configuration from server config."""
    config_file = "llama_stack_config.yaml"
    
    if not os.path.exists(config_file):
        raise FileNotFoundError(f"Configuration file {config_file} not found")
    
    try:
        with open(config_file, 'r') as f:
            server_config = yaml.safe_load(f)
            
        # Look for config in agents.providers section
        if 'providers' in server_config and 'agents' in server_config['providers']:
            agents_providers = server_config['providers']['agents']
            
            # Find the meta-reference provider config
            for provider in agents_providers:
                if provider.get('provider_type') == 'inline::meta-reference':
                    if 'config' in provider and config_name in provider['config']:
                        agent_config = provider['config'][config_name].copy()
                        
                        # Handle environment variable substitution for model
                        if 'model' in agent_config:
                            model = agent_config['model']
                            if isinstance(model, str) and model.startswith('${env.'):
                                env_var = model.split('${env.')[1].split(':')[0].rstrip('}')
                                default_val = model.split('=')[1].rstrip('}') if '=' in model else None
                                if default_val:
                                    agent_config['model'] = os.getenv(env_var, default_val)
                                else:
                                    agent_config['model'] = os.getenv(env_var)
                                    if not agent_config['model']:
                                        raise ValueError(f"Environment variable {env_var} not set and no default provided")
                        
                        return agent_config
                        
        raise ValueError(f"Agent configuration '{config_name}' not found in {config_file}")
        
    except yaml.YAMLError as e:
        raise ValueError(f"Error parsing YAML configuration: {e}")
    except Exception as e:
        raise ValueError(f"Error loading agent configuration '{config_name}': {e}")

def wait_for_session(client, session_id, agent_id, timeout=1800):
    """Wait for a session to be created by another container."""
    print(f"⏳ Waiting for session {session_id} to be available...")
    
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            session = client.agents.session.retrieve(
                session_id=session_id,
                agent_id=agent_id
            )
            if session:
                print(f"✅ Session {session_id} found!")
                return session
        except Exception as e:
            # Session doesn't exist yet, wait and retry
            pass
        
        time.sleep(5)  # Wait 5 seconds before retry
    
    raise TimeoutError(f"Session {session_id} not found after {timeout} seconds")

def get_session_output(client, session_id, agent_id):
    """Get the output from a completed session."""
    session = client.agents.session.retrieve(
        session_id=session_id,
        agent_id=agent_id
    )
    
    # Get the last turn's output
    if session.turns:
        last_turn = session.turns[-1]
        if hasattr(last_turn, 'output_message') and last_turn.output_message:
            return last_turn.output_message.content
    
    return None

def run_jira_analyzer():
    """Run Jira analyzer agent - first in the chain."""
    issue = os.getenv('ISSUE_ID')
    if not issue:
        print("❌ ISSUE_ID environment variable not set")
        sys.exit(1)
    
    print(f"🤖 [Jira Container] Starting jira_analyzer for issue: {issue}")
    
    try:
        llama_stack_url = os.getenv('LLAMA_STACK_URL', 'http://localhost:8321')
        client = LlamaStackClient(base_url=llama_stack_url)
        
        jira_config = load_agent_config("jira_analyzer")
        jira_agent = Agent(client, **jira_config)
        
        # Create session with predictable name for other containers
        session_id = f"jira-analysis-{issue}"
        jira_session_id = jira_agent.create_session(session_name=session_id)
        
        # Store session info for other containers
        session_info = {
            'session_id': jira_session_id,
            'agent_id': jira_agent.agent_id,
            'issue': issue,
            'status': 'running'
        }
        
        with open('/shared/jira_session_info.json', 'w') as f:
            json.dump(session_info, f)
        
        print(f"📋 [Jira Container] Session info saved: {session_info}")
        
        # Run the analysis
        jira_response = jira_agent.create_turn(
            messages=[{"role": "user", "content": f"Analyze Jira issue {issue}"}],
            session_id=jira_session_id,
            stream=False,
        )
        
        # Mark as complete
        session_info['status'] = 'complete'
        with open('/shared/jira_session_info.json', 'w') as f:
            json.dump(session_info, f)
        
        print(f"✅ [Jira Container] Analysis complete for issue: {issue}")
        
    except Exception as e:
        print(f"❌ [Jira Container] Error: {e}")
        # Mark as failed
        session_info = {
            'session_id': jira_session_id if 'jira_session_id' in locals() else None,
            'agent_id': jira_agent.agent_id if 'jira_agent' in locals() else None,
            'issue': issue,
            'status': 'failed',
            'error': str(e)
        }
        
        with open('/shared/jira_session_info.json', 'w') as f:
            json.dump(session_info, f)
        
        sys.exit(1)

def run_rebase_package():
    """Run package rebase agent - second in the chain."""
    issue = os.getenv('ISSUE_ID')
    if not issue:
        print("❌ ISSUE_ID environment variable not set")
        sys.exit(1)
    
    print(f"🤖 [Rebase Container] Starting rebase_package for issue: {issue}")
    
    try:
        llama_stack_url = os.getenv('LLAMA_STACK_URL', 'http://localhost:8321')
        client = LlamaStackClient(base_url=llama_stack_url)
        
        # Wait for jira session info
        print("⏳ [Rebase Container] Waiting for Jira analysis to complete...")
        timeout = 1800  # 30 minutes
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if os.path.exists('/shared/jira_session_info.json'):
                with open('/shared/jira_session_info.json', 'r') as f:
                    jira_info = json.load(f)
                
                if jira_info['status'] == 'complete':
                    print("✅ [Rebase Container] Jira analysis completed!")
                    break
                elif jira_info['status'] == 'failed':
                    print(f"❌ [Rebase Container] Jira analysis failed: {jira_info.get('error')}")
                    sys.exit(1)
            
            time.sleep(5)
        else:
            print("⏰ [Rebase Container] Timeout waiting for Jira analysis")
            sys.exit(1)
        
        # Get the Jira analysis output from the session
        jira_analysis = get_session_output(
            client, 
            jira_info['session_id'], 
            jira_info['agent_id']
        )
        
        if not jira_analysis:
            print("❌ [Rebase Container] Could not retrieve Jira analysis")
            sys.exit(1)
        
        print(f"📋 [Rebase Container] Retrieved Jira analysis: {len(jira_analysis)} characters")
        
        # Run rebase workflow
        rebase_config = load_agent_config("rebase_package")
        rebase_agent = Agent(client, **rebase_config)
        rebase_session_id = rebase_agent.create_session(session_name=f"rebase-{issue}")
        
        # Chain the results
        chained_message = f"""Jira issue analysis results:
{jira_analysis}

Please proceed with the package rebase workflow for issue {issue}."""
        
        print(f"🤖 [Rebase Container] Starting package rebase workflow...")
        rebase_response = rebase_agent.create_turn(
            messages=[{"role": "user", "content": chained_message}],
            session_id=rebase_session_id,
            stream=True,
        )
        
        # Process streaming response
        for event in rebase_response:
            if hasattr(event, 'event') and hasattr(event.event, 'payload'):
                payload = event.event.payload
                if hasattr(payload, 'delta') and hasattr(payload.delta, 'text'):
                    print(payload.delta.text, end="", flush=True)
                elif hasattr(payload, 'event_type') and payload.event_type == 'turn_complete':
                    print(f"\n\n✅ [Rebase Container] Package rebase complete")
                    break
        
        # Save completion info
        completion_info = {
            'jira_session_id': jira_info['session_id'],
            'jira_agent_id': jira_info['agent_id'],
            'rebase_session_id': rebase_session_id,
            'rebase_agent_id': rebase_agent.agent_id,
            'issue': issue,
            'status': 'complete'
        }
        
        with open('/shared/workflow_complete.json', 'w') as f:
            json.dump(completion_info, f)
        
        print(f"🎉 [Rebase Container] Workflow complete for issue: {issue}")
        
    except Exception as e:
        print(f"❌ [Rebase Container] Error: {e}")
        sys.exit(1)

def main():
    """Main entry point based on AGENT_TYPE environment variable."""
    agent_type = os.getenv('AGENT_TYPE')
    
    if agent_type == 'jira_analyzer':
        run_jira_analyzer()
    elif agent_type == 'rebase_package':
        run_rebase_package()
    else:
        print(f"❌ Unknown AGENT_TYPE: {agent_type}")
        print("Valid options: jira_analyzer, rebase_package")
        sys.exit(1)

if __name__ == "__main__":
    main() 