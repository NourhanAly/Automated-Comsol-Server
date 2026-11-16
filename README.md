# Automated-Comsol-Server
The purpose of this server is to allow commination between the working battery and the predictive COMSOL model, seeing as the model could provide valuable parameters concerning the state of health of the battery.

two sides are considered: local desktop with Comsol installed (Comsol server side) and postgresql database as the cloud to be communicated with.

The steps of the server are mainly: Downloading input data from the Postgresql database, running the COMSOL modelling flow according to the input to obtain the desired output and finally sending the output files to the database.


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

        |
        v
PostgreSQL Database
(Stores predictions and comparisons)
