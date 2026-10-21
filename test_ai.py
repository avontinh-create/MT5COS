from app.services.ai_engine.optimizer import AIOptimizer

if __name__ == "__main__":
    optimizer = AIOptimizer("terminal64.exe", "./workspace")
    best = optimizer.run_optimization(n_trials=20)
    print("\nThông số tốt nhất:", best)
