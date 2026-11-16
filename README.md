# Automated-Comsol-Server

This repository implements the communication layer between a predictive COMSOL battery model and experimental battery data. The framework enables automated estimation of battery state-of-health (SOH) parameters by coupling laboratory measurements with a physics-based simulation model.
The system consists of two main components:

- A local workstation running COMSOL and the Python automation server.
- A PostgreSQL database used to store experimental measurements and model outputs.
The server workflow consists of retrieving battery measurements from the PostgreSQL database, running the COMSOL model with the corresponding battery conditions, and uploading the resulting SOH predictions and model outputs back to the database.



# Server Flow
```text
Battery Tester (Experimental measurements)
        |
        v
PostgreSQL Database (Stores battery test data)
        |
        v
Python Automation Layer (Data retrieval and preprocessing)
        |
        v
COMSOL Battery Model
(Run model with current battery conditions)
        |
        v
Predicted State-of-Health (SOH)
and Model/Measurement Comparison
        |
        v
Python Automation Layer (Result processing and upload)
        |
        v
PostgreSQL Database
(Storing model outputs and predictions)
```

