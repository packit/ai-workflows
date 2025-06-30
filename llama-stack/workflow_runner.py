#!/usr/bin/env python3
"""
Simple Jira Issue Analyzer using Llama Stack Agent class
# for more workflows, see https://github.com/meta-llama/llama-stack/blob/main/docs/notebooks/Llama_Stack_Agent_Workflows.ipynb
"""

import sys
import os
import yaml
from pprint import pprint
from llama_stack_client import LlamaStackClient, Agent

def load_agent_config(config_name):
    """Load agent configuration from server config or throw exception if not found."""
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
                                # Simple env var substitution
                                env_var = model.split('${env.')[1].split(':')[0].rstrip('}')
                                default_val = model.split('=')[1].rstrip('}') if '=' in model else None
                                if default_val:
                                    agent_config['model'] = os.getenv(env_var, default_val)
                                else:
                                    agent_config['model'] = os.getenv(env_var)
                                    if not agent_config['model']:
                                        raise ValueError(f"Environment variable {env_var} not set and no default provided")
                        
                        return agent_config
                        
        # If we get here, the agent config was not found
        raise ValueError(f"Agent configuration '{config_name}' not found in {config_file}")
        
    except yaml.YAMLError as e:
        raise ValueError(f"Error parsing YAML configuration: {e}")
    except Exception as e:
        raise ValueError(f"Error loading agent configuration '{config_name}': {e}")

def main():
    """Run chained agent workflow: Jira analysis -> Package rebase."""
    if len(sys.argv) < 2:
        print("Usage: python workflow_runner.py <ISSUE_ID>")
        print("Example: python workflow_runner.py RHEL-78418")
        sys.exit(1)
    
    issue = sys.argv[1]
    
    try:
        # Initialize client
        client = LlamaStackClient(base_url="http://localhost:8321")
        
        # STEP 1: Analyze Jira issue
        print(f"🤖 Starting jira_analyzer agent for issue: {issue}")
        jira_config = load_agent_config("jira_analyzer")

        client.toolgroups.register(
            toolgroup_id="mcp::jira",
            provider_id="model-context-protocol",
            mcp_endpoint={"uri": "http://localhost:9000/sse"},
        )
        
        #jira_agent = Agent(client, tools=["mcp::jira"], **jira_config) # access mcp jira server
        jira_agent = Agent(client, **jira_config) # use training data instead of mcp jira server
        jira_session_id = jira_agent.create_session(session_name=f"analyze-{issue}")
        
        jira_response = jira_agent.create_turn(
            messages=[{"role": "user", "content": f"Analyze Jira issue {issue}"}],
            session_id=jira_session_id,
            stream=False,
        )
        
        # Retrieve and print session
        jira_session = client.agents.session.retrieve(
            session_id=jira_session_id, 
            agent_id=jira_agent.agent_id
        )
        
        print("📋 Jira Analysis Session:")
        pprint(jira_session.to_dict())
        
        # Extract analysis result
        jira_analysis = jira_response.output_message.content
        print(f"\n✅ Jira analysis complete")
        
        # STEP 2: Package rebase workflow
        print(f"\n🤖 Starting rebase_package agent for issue: {issue}")
        rebase_config = load_agent_config("rebase_package")
        rebase_agent = Agent(client, **rebase_config)
        rebase_session_id = rebase_agent.create_session(session_name=f"rebase-{issue}")
        
        # Chain the results
        chained_message = f"""Jira issue analysis results:
{jira_analysis}

Please proceed with the package rebase workflow for issue {issue}."""
        
        print(f"🤖 Package Rebase Workflow:")
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
                    print(f"\n\n✅ Package rebase workflow complete")
                    break
        
        # Retrieve and print session
        rebase_session = client.agents.session.retrieve(
            session_id=rebase_session_id, 
            agent_id=rebase_agent.agent_id
        )
        
        print("\n📦 Package Rebase Session:")
        pprint(rebase_session.to_dict())
        
        print(f"\n🎉 Workflow complete for issue: {issue}")
        
    except Exception as e:
        if "ConnectionError" in str(type(e)):
            print("❌ Cannot connect to Llama Stack. Run 'make start-local-stack' first.")
        else:
            print(f"❌ Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 