#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autonomous_engine.py - Autonomous Rule Engine and Decision Making

Implements autonomous capabilities for tactical operations:
- Rule-based automation
- Event triggers and actions
- Scheduled tasks
- Conditional logic
"""

import json
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Callable
import threading
import time
from database import SessionLocal
from models import AutonomousRule as RuleModel

logger = logging.getLogger("lpu5-autonomous")


class Rule:
    """Represents an autonomous rule"""
    
    def __init__(self,
                 rule_id: str,
                 name: str,
                 description: str,
                 trigger_type: str,  # geofence, time, status_change, message, manual
                 trigger_config: Dict[str, Any],
                 conditions: List[Dict[str, Any]],
                 actions: List[Dict[str, Any]],
                 enabled: bool = True,
                 priority: int = 5):
        """
        Initialize an autonomous rule
        
        Args:
            rule_id: Unique identifier
            name: Rule name
            description: Rule description
            trigger_type: Type of trigger (geofence, time, status_change, message, manual)
            trigger_config: Trigger configuration
            conditions: List of conditions to check
            actions: List of actions to execute
            enabled: Whether rule is active
            priority: Execution priority (1-10, higher = more important)
        """
        self.rule_id = rule_id
        self.name = name
        self.description = description
        self.trigger_type = trigger_type
        self.trigger_config = trigger_config
        self.conditions = conditions
        self.actions = actions
        self.enabled = enabled
        self.priority = priority
        self.created_at = datetime.utcnow().isoformat()
        self.last_triggered = None
        self.execution_count = 0
        
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "description": self.description,
            "trigger_type": self.trigger_type,
            "trigger_config": self.trigger_config,
            "conditions": self.conditions,
            "actions": self.actions,
            "enabled": self.enabled,
            "priority": self.priority,
            "created_at": self.created_at,
            "last_triggered": self.last_triggered,
            "execution_count": self.execution_count
        }
    
    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'Rule':
        """Create from dictionary"""
        rule = Rule(
            rule_id=data["rule_id"],
            name=data["name"],
            description=data["description"],
            trigger_type=data["trigger_type"],
            trigger_config=data["trigger_config"],
            conditions=data["conditions"],
            actions=data["actions"],
            enabled=data.get("enabled", True),
            priority=data.get("priority", 5)
        )
        rule.created_at = data.get("created_at", rule.created_at)
        rule.last_triggered = data.get("last_triggered")
        rule.execution_count = data.get("execution_count", 0)
        return rule


class AutonomousEngine:
    """Autonomous rule engine for tactical operations"""
    
    def __init__(self, db_path: str = "autonomous_rules_db.json"):
        """
        Initialize autonomous engine
        
        Args:
            db_path: Path to rules database file
        """
        self.db_path = db_path
        self.rules: Dict[str, Rule] = {}
        self.action_handlers: Dict[str, Callable] = {}
        self.condition_evaluators: Dict[str, Callable] = {}
        self.running = False
        self.worker_thread = None
        self.load_rules()
        self._register_default_handlers()
        
    def load_rules(self):
        """Load rules from SQLAlchemy database"""
        db = SessionLocal()
        try:
            db_rules = db.query(RuleModel).all()
            self.rules = {}
            for db_rule in db_rules:
                # Convert DB model to legacy class for runtime logic
                rule = Rule(
                    rule_id=db_rule.id,
                    name=db_rule.name,
                    description=db_rule.description,
                    trigger_type=db_rule.trigger_type,
                    trigger_config=db_rule.trigger_config,
                    conditions=db_rule.conditions,
                    actions=db_rule.actions,
                    enabled=db_rule.enabled,
                    priority=db_rule.priority
                )
                rule.created_at = db_rule.created_at.isoformat() if db_rule.created_at else rule.created_at
                rule.last_triggered = db_rule.last_triggered.isoformat() if db_rule.last_triggered else None
                rule.execution_count = db_rule.execution_count or 0
                self.rules[rule.rule_id] = rule
            logger.info(f"Loaded {len(self.rules)} autonomous rules from database")
        except Exception as e:
            logger.error(f"Failed to load rules from DB: {e}")
        finally:
            db.close()
    
    def save_rules(self):
        """Not needed for DB version, changes are committed individually. Re-syncing for safety."""
        self.load_rules()
    
    def create_rule(self, rule: Rule) -> Rule:
        """Create a new rule (DB-backed)"""
        db = SessionLocal()
        try:
            db_rule = RuleModel(
                id=rule.rule_id,
                name=rule.name,
                description=rule.description,
                trigger_type=rule.trigger_type,
                trigger_config=rule.trigger_config,
                conditions=rule.conditions,
                actions=rule.actions,
                enabled=rule.enabled,
                priority=rule.priority,
                execution_count=rule.execution_count,
                data=rule.to_dict()
            )
            db.add(db_rule)
            db.commit()
            
            # Update local cache
            self.rules[rule.rule_id] = rule
            logger.info(f"Created rule in DB: {rule.name} ({rule.rule_id})")
            return rule
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to create rule in DB: {e}")
            raise
        finally:
            db.close()
    
    def update_rule(self, rule_id: str, updates: Dict[str, Any]) -> Optional[Rule]:
        """Update an existing rule (DB-backed)"""
        db = SessionLocal()
        try:
            db_rule = db.query(RuleModel).filter(RuleModel.id == rule_id).first()
            if not db_rule:
                return None
            
            for key, value in updates.items():
                if hasattr(db_rule, key) and key != 'id':
                    setattr(db_rule, key, value)
            
            db.commit()
            
            # Re-read to refresh cache
            self.load_rules()
            return self.rules.get(rule_id)
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to update rule in DB: {e}")
            return None
        finally:
            db.close()
    
    def delete_rule(self, rule_id: str) -> bool:
        """Delete a rule (DB-backed)"""
        db = SessionLocal()
        try:
            db_rule = db.query(RuleModel).filter(RuleModel.id == rule_id).first()
            if db_rule:
                db.delete(db_rule)
                db.commit()
                if rule_id in self.rules:
                    self.rules.pop(rule_id)
                logger.info(f"Deleted rule from DB: {rule_id}")
                return True
            return False
        except Exception as e:
            db.rollback()
            logger.error(f"Failed to delete rule from DB: {e}")
            return False
        finally:
            db.close()
    
    def get_rule(self, rule_id: str) -> Optional[Rule]:
        """Get a rule by ID"""
        return self.rules.get(rule_id)
    
    def list_rules(self, enabled_only: bool = False) -> List[Rule]:
        """List all rules"""
        rules = list(self.rules.values())
        if enabled_only:
            rules = [r for r in rules if r.enabled]
        # Sort by priority (descending)
        rules.sort(key=lambda x: x.priority, reverse=True)
        return rules
    
    def register_action_handler(self, action_type: str, handler: Callable):
        """
        Register a handler for an action type
        
        Args:
            action_type: Type of action
            handler: Callable that takes (action_config, context) and executes the action
        """
        self.action_handlers[action_type] = handler
        logger.info(f"Registered action handler: {action_type}")
    
    def register_condition_evaluator(self, condition_type: str, evaluator: Callable):
        """
        Register an evaluator for a condition type
        
        Args:
            condition_type: Type of condition
            evaluator: Callable that takes (condition_config, context) and returns bool
        """
        self.condition_evaluators[condition_type] = evaluator
        logger.info(f"Registered condition evaluator: {condition_type}")
    
    def evaluate_conditions(self, conditions: List[Dict[str, Any]], context: Dict[str, Any]) -> bool:
        """
        Evaluate all conditions for a rule
        
        Args:
            conditions: List of condition configurations
            context: Execution context
            
        Returns:
            True if all conditions pass, False otherwise
        """
        if not conditions:
            return True
        
        for condition in conditions:
            condition_type = condition.get("type")
            if condition_type not in self.condition_evaluators:
                logger.warning(f"No evaluator for condition type: {condition_type}")
                return False
            
            evaluator = self.condition_evaluators[condition_type]
            try:
                if not evaluator(condition, context):
                    return False
            except Exception as e:
                logger.error(f"Condition evaluation failed: {e}")
                return False
        
        return True
    
    def execute_actions(self, actions: List[Dict[str, Any]], context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Execute all actions for a rule
        
        Args:
            actions: List of action configurations
            context: Execution context
            
        Returns:
            List of action results
        """
        results = []
        
        for action in actions:
            action_type = action.get("type")
            if action_type not in self.action_handlers:
                logger.warning(f"No handler for action type: {action_type}")
                results.append({
                    "action_type": action_type,
                    "success": False,
                    "error": f"No handler for action type: {action_type}"
                })
                continue
            
            handler = self.action_handlers[action_type]
            try:
                result = handler(action, context)
                results.append({
                    "action_type": action_type,
                    "success": True,
                    "result": result
                })
                logger.info(f"Executed action: {action_type}")
            except Exception as e:
                logger.error(f"Action execution failed: {e}")
                results.append({
                    "action_type": action_type,
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def trigger_rules(self, trigger_type: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Trigger rules matching a specific trigger type
        
        Args:
            trigger_type: Type of trigger
            context: Trigger context
            
        Returns:
            List of execution results
        """
        results = []
        
        # Get matching rules
        matching_rules = [
            rule for rule in self.rules.values()
            if rule.enabled and rule.trigger_type == trigger_type
        ]
        
        # Sort by priority
        matching_rules.sort(key=lambda x: x.priority, reverse=True)
        
        for rule in matching_rules:
            try:
                # Check conditions
                if not self.evaluate_conditions(rule.conditions, context):
                    logger.debug(f"Rule conditions not met: {rule.name}")
                    continue
                
                # Execute actions
                action_results = self.execute_actions(rule.actions, context)
                
                # Update rule stats in DB
                db = SessionLocal()
                try:
                    db_rule = db.query(RuleModel).filter(RuleModel.id == rule.rule_id).first()
                    if db_rule:
                        db_rule.last_triggered = datetime.utcnow()
                        db_rule.execution_count += 1
                        db.commit()
                    
                    # Also update local cache
                    rule.last_triggered = datetime.utcnow().isoformat()
                    rule.execution_count += 1
                except Exception as db_err:
                    db.rollback()
                    logger.error(f"Failed to update rule stats in DB: {db_err}")
                finally:
                    db.close()
                
                results.append({
                    "rule_id": rule.rule_id,
                    "rule_name": rule.name,
                    "success": True,
                    "action_results": action_results,
                    "timestamp": rule.last_triggered
                })
                
                logger.info(f"Executed rule: {rule.name}")
                
            except Exception as e:
                logger.error(f"Rule execution failed: {e}")
                results.append({
                    "rule_id": rule.rule_id,
                    "rule_name": rule.name,
                    "success": False,
                    "error": str(e)
                })
        
        return results
    
    def start_scheduler(self):
        """Start the time-based rule scheduler"""
        if self.running:
            logger.warning("Scheduler already running")
            return
        
        self.running = True
        self.worker_thread = threading.Thread(target=self._scheduler_worker, daemon=True)
        self.worker_thread.start()
        logger.info("Autonomous scheduler started")
    
    def stop_scheduler(self):
        """Stop the scheduler"""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)
        logger.info("Autonomous scheduler stopped")
    
    def _scheduler_worker(self):
        """Worker thread for time-based rules"""
        while self.running:
            try:
                now = datetime.utcnow()
                
                # Check time-based rules
                for rule in self.rules.values():
                    if not rule.enabled or rule.trigger_type != "time":
                        continue
                    
                    trigger_config = rule.trigger_config
                    schedule_type = trigger_config.get("schedule_type", "interval")
                    
                    should_trigger = False
                    
                    if schedule_type == "interval":
                        interval_seconds = trigger_config.get("interval_seconds", 60)
                        if rule.last_triggered:
                            last_time = datetime.fromisoformat(rule.last_triggered)
                            if (now - last_time).total_seconds() >= interval_seconds:
                                should_trigger = True
                        else:
                            should_trigger = True
                    
                    elif schedule_type == "daily":
                        target_time = trigger_config.get("time", "00:00")
                        target_hour, target_minute = map(int, target_time.split(":"))
                        
                        if now.hour == target_hour and now.minute == target_minute:
                            if not rule.last_triggered or \
                               datetime.fromisoformat(rule.last_triggered).date() != now.date():
                                should_trigger = True
                    
                    if should_trigger:
                        context = {
                            "trigger_type": "time",
                            "timestamp": now.isoformat(),
                            "rule_id": rule.rule_id
                        }
                        self.trigger_rules("time", context)
                
                # Sleep for a minute before next check
                time.sleep(60)
                
            except Exception as e:
                logger.error(f"Scheduler worker error: {e}")
                time.sleep(60)
    
    def _register_default_handlers(self):
        """Register default condition evaluators and action handlers"""
        
        # Condition: Compare value
        def evaluate_compare(condition: Dict, context: Dict) -> bool:
            value = context.get(condition.get("field"))
            operator = condition.get("operator", "==")
            compare_to = condition.get("value")
            
            if operator == "==":
                return value == compare_to
            elif operator == "!=":
                return value != compare_to
            elif operator == ">":
                return value > compare_to
            elif operator == "<":
                return value < compare_to
            elif operator == ">=":
                return value >= compare_to
            elif operator == "<=":
                return value <= compare_to
            elif operator == "in":
                return value in compare_to
            elif operator == "not_in":
                return value not in compare_to
            return False
        
        self.register_condition_evaluator("compare", evaluate_compare)
        
        # Action: Log message
        def action_log(action: Dict, context: Dict) -> str:
            message = action.get("message", "Rule triggered")
            level = action.get("level", "info")
            
            if level == "info":
                logger.info(message)
            elif level == "warning":
                logger.warning(message)
            elif level == "error":
                logger.error(message)
            
            return message
        
        self.register_action_handler("log", action_log)
        
        # Action: Set status
        def action_set_status(action: Dict, context: Dict) -> Dict:
            entity_id = action.get("entity_id") or context.get("entity_id")
            status = action.get("status")
            
            return {
                "entity_id": entity_id,
                "status": status,
                "timestamp": datetime.utcnow().isoformat()
            }
        
        self.register_action_handler("set_status", action_set_status)
